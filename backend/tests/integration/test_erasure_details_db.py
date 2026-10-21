"""#2244: account erasure puts back the details an erased editor wrote.

Against a real Postgres. Where the erased subject made the newest ``details``
edit of a memory another member authored, the live ``details`` goes back to
the last value someone else wrote, with a version bump and one ``erasure``
revision row; the vector payload's ``location`` follows before the commit.

1. the restore, then the rest of the erasure's memory steps, on the author's
   memory: live value, version, last writer, the new row, the rebuilt
   versions, the payload patch (``location`` set);
2. the subject's consecutive edits are walked past (and the strip still
   removes their earlier value from the log);
3. a double erasure: the value to go back to was removed earlier → NULL,
   ``location`` cleared from the payload;
4. a tombstone is restored in Postgres with no payload call;
5. a payload failure raises and leaves Postgres as it was after the
   rollback; a row not embedded yet is re-queued instead of patched;
6. every row outside the subject's scope is byte-identical afterwards, the
   subject's own memory gets no ``erasure`` row, and a memory someone else
   edited after the subject is not restored;
7. a re-run by pseudonym (#2243) and a dry run change nothing — a strip by
   pseudonym does not take the erasure row for the subject's edit; a live
   value already equal to the target is not rewritten.

The vector store is stubbed. Every seeded row goes with its workspace.
Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.auth import Context, Workspace
from models.memory import Memory, MemoryRevision
from services.account_erasure_service import AccountErasureService
from services.erasure_details import (
    RestoreReport,
    StripCounts,
    lock_subject_memories,
    restore_editor_details,
    strip_subject_details,
)
from services.memory_versions import load_version
from utils.datetime import utcnow
from utils.exceptions import QdrantError

pytestmark = pytest.mark.asyncio(loop_scope="session")

_QDRANT = "services.erasure_details.update_memory_payload_in_qdrant"

CREATION = {"at": "creation"}
AUTHORS = {"by": "author", "location": {"lat": 10.5, "lon": 20.25}}
SUBJECTS = {"by": "subject", "location": {"lat": 35.0, "lon": 139.0}}
SUBJECTS_2 = {"by": "subject-again", "secret": "x"}


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """A committed workspace and context, and the names of the members."""
    tag = uuid.uuid4().hex[:8]
    ws_id, ctx_id = uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        session.add(Workspace(id=ws_id, name=f"ws-2244-{tag}", owner_user_id=f"author-{tag}"))
        await session.flush()
        session.add(Context(id=ctx_id, workspace_id=ws_id, name="2244", created_by=f"author-{tag}"))
        await session.commit()
    try:
        yield {
            "workspace_id": ws_id,
            "context_id": ctx_id,
            "subject": f"subject-{tag}",
            "author": f"author-{tag}",
            "third": f"third-{tag}",
            "fourth": f"fourth-{tag}",
        }
    finally:
        async with _maker(async_engine)() as session:
            # contexts → memories → memory_revisions cascade with the workspace
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()


def _memory(
    world: dict[str, Any],
    owner: str,
    details: dict[str, Any] | None,
    version: int,
    *,
    session_id: str | None = None,
    deleted: bool = False,
    embedding_status: str = "success",
) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=owner,
        workspace_id=world["workspace_id"],
        context_id=world["context_id"],
        summary="a memory",
        content="body",
        type="note",
        client="test",
        details=details,
        version=version,
        session_id=session_id,
        agent_id=uuid.uuid4() if session_id else None,
        embedding_status=embedding_status,
        deleted_at=utcnow() if deleted else None,
    )


def _edit(
    world: dict[str, Any],
    memory: Memory,
    version: int,
    actor: str,
    prior: dict[str, Any],
    session_id: str | None = None,
) -> MemoryRevision:
    return MemoryRevision(
        memory_id=memory.id,
        context_id=world["context_id"],
        version=version,
        kind="updated",
        writer="api",
        actor_user_id=actor,
        prior=prior,
        session_id=session_id,
    )


async def _seed(session: AsyncSession, memories: list[Memory], edits: list[MemoryRevision]) -> None:
    session.add_all(memories)
    await session.flush()
    session.add_all(edits)
    await session.commit()


def _put_back(restored_from: int) -> dict[str, Any]:
    """The prior of the erasure row: details unavailable, and the version of
    the lowest edit of the block it reverted."""
    return {"erased": ["details"], "restored_from": restored_from}


def _service(session: AsyncSession) -> AccountErasureService:
    service = AccountErasureService.__new__(AccountErasureService)
    service.db = session
    return service


async def _erase_memories(session: AsyncSession, subject: str) -> tuple[str, RestoreReport]:
    """The memory steps of ``_delete_postgres``, in its order, then commit."""
    service = _service(session)
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        pseudonym = service._pseudonym(subject)
        await lock_subject_memories(session, subject)
        report = await restore_editor_details(session, subject, actor=pseudonym)
        await service._erase_session_provenance(subject)
        await service._erase_memory_revisions(subject)
        await service._scrub_surviving_memories(subject)
    await session.commit()
    return pseudonym, report


async def _log(session: AsyncSession, *memory_ids: uuid.UUID) -> list[tuple[Any, ...]]:
    """``(memory_id, version, writer, actor, prior, session_id)`` per row."""
    rows = (
        await session.execute(
            select(
                MemoryRevision.memory_id,
                MemoryRevision.version,
                MemoryRevision.writer,
                MemoryRevision.actor_user_id,
                MemoryRevision.prior,
                MemoryRevision.session_id,
            ).where(MemoryRevision.memory_id.in_(memory_ids))
        )
    ).all()
    return sorted((tuple(r) for r in rows), key=lambda r: (memory_ids.index(r[0]), r[1]))


async def _live(session: AsyncSession, memory_id: uuid.UUID) -> Memory:
    return (
        await session.execute(
            select(Memory).where(Memory.id == memory_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


# Whole-table byte-level snapshots (every tenant), keyed by id.
_REVISIONS_OUTSIDE = text(
    "SELECT t.id, to_jsonb(t) FROM memory_revisions AS t WHERE NOT (t.memory_id = ANY(:excluded))"
)
_MEMORIES_OUTSIDE = text(
    "SELECT t.id, to_jsonb(t) FROM memories AS t WHERE NOT (t.id = ANY(:excluded))"
)


async def _snapshot(session: AsyncSession, query: Any, excluded: list[uuid.UUID]) -> dict:
    rows = (await session.execute(query, {"excluded": excluded})).all()
    return {r[0]: r[1] for r in rows}


async def test_the_authors_value_comes_back_with_its_own_revision(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 4, session_id="sess-subject")
    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [memory],
            [
                _edit(world, memory, 2, author, {"details": CREATION}),
                _edit(world, memory, 3, author, {"importance": 0.4}),
                _edit(world, memory, 4, subject, {"details": AUTHORS}, "sess-subject"),
            ],
        )
        before = (await _live(session, memory.id)).updated_at
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            pseudonym, report = await _erase_memories(session, subject)

        assert report == RestoreReport(
            restored=1, unavailable=0, payload_synced=1, payload_skipped=0, memory_ids=[memory.id]
        )
        live = await _live(session, memory.id)
        assert live.details == AUTHORS
        assert live.version == 5
        assert live.updated_at is not None and live.updated_at != before
        assert (live.agent_id, live.session_id) == (None, None)
        assert live.user_id == author
        assert await _log(session, memory.id) == [
            (memory.id, 2, "api", author, {"details": CREATION}, None),
            (memory.id, 3, "api", author, {"importance": 0.4}, None),
            # the subject's own row holds the author's value: kept, pseudonymized
            (memory.id, 4, "api", pseudonym, {"details": AUTHORS}, None),
            (memory.id, 5, "erasure", pseudonym, _put_back(4), None),
        ]
        # the subject's version reads "unavailable"; the author's reads back
        at_4 = await load_version(session, live, 4)
        assert at_4.unavailable == ("details",) and at_4.values["details"] is None
        assert (await load_version(session, live, 3)).values["details"] == AUTHORS
        assert (await load_version(session, live, 1)).values["details"] == CREATION

    sync.assert_awaited_once()
    kwargs = sync.await_args.kwargs
    assert kwargs["memory_id"] == memory.id
    assert kwargs["payload_updates"]["location"] == {"lat": 10.5, "lon": 20.25}
    assert set(kwargs["payload_updates"]) == {"location", "updated_at"}
    assert kwargs["delete_keys"] is None
    assert kwargs["cascade_chunks"] is False  # chunks are off for the context
    assert kwargs["collection_name"]


async def test_the_subjects_earlier_edits_are_walked_past(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS_2, 5)
    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [memory],
            [
                _edit(world, memory, 2, author, {"details": CREATION}),
                _edit(world, memory, 3, subject, {"details": AUTHORS}),
                _edit(world, memory, 4, subject, {"summary": "not details"}),
                _edit(world, memory, 5, subject, {"details": SUBJECTS}),
            ],
        )
        with patch(_QDRANT, AsyncMock()):
            pseudonym, report = await _erase_memories(session, subject)

        assert (report.restored, report.unavailable) == (1, 0)
        assert (await _live(session, memory.id)).details == AUTHORS  # not SUBJECTS
        assert await _log(session, memory.id) == [
            (memory.id, 2, "api", author, {"details": CREATION}, None),
            (memory.id, 3, "api", pseudonym, {"details": AUTHORS}, None),
            (memory.id, 4, "api", pseudonym, {"summary": "not details"}, None),
            # the subject's first value: stripped from the log as before
            (memory.id, 5, "api", pseudonym, {"erased": ["details"]}, None),
            (memory.id, 6, "erasure", pseudonym, _put_back(3), None),
        ]


async def test_a_value_an_earlier_erasure_removed_gives_null(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """Double erasure: a member erased earlier wrote the value the subject
    overwrote; the earlier erasure stripped it from the subject's prior."""
    subject, author, ghost = world["subject"], world["author"], "e" * 64
    memory = _memory(world, author, SUBJECTS, 3)
    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [memory],
            [
                _edit(world, memory, 2, ghost, {"details": CREATION}),
                _edit(world, memory, 3, subject, {"erased": ["details"]}),
            ],
        )
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            pseudonym, report = await _erase_memories(session, subject)

        assert (report.restored, report.unavailable) == (0, 1)
        live = await _live(session, memory.id)
        assert live.details is None
        assert live.version == 4
        assert (await _log(session, memory.id))[-1] == (
            memory.id,
            4,
            "erasure",
            pseudonym,
            _put_back(3),
            None,
        )
    kwargs = sync.await_args.kwargs
    assert set(kwargs["payload_updates"]) == {"updated_at"}
    assert kwargs["delete_keys"] == ["location"]


async def test_a_tombstone_is_restored_without_a_payload_call(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """``forget`` keeps the log and an un-forget would bring the subject's
    value back, so the tombstone is restored too; its point is not patched."""
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 2, deleted=True)
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            pseudonym, report = await _erase_memories(session, subject)

        assert report == RestoreReport(
            restored=1, payload_synced=0, payload_skipped=1, memory_ids=[memory.id]
        )
        live = await _live(session, memory.id)
        assert (live.details, live.version) == (AUTHORS, 3)
        assert live.deleted_at is not None
        assert (await _log(session, memory.id))[-1][1:4] == (3, "erasure", pseudonym)
    sync.assert_not_awaited()


async def test_a_payload_failure_raises_and_postgres_rolls_back(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 2)
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        memory_id = memory.id  # the rollback below expires the instance
        everything = [uuid.uuid4()]  # exclude nothing
        memories_before = await _snapshot(session, _MEMORIES_OUTSIDE, everything)
        revisions_before = await _snapshot(session, _REVISIONS_OUTSIDE, everything)
        await session.commit()

        with patch(_QDRANT, AsyncMock(side_effect=QdrantError("vector store down"))):
            with pytest.raises(QdrantError):
                await restore_editor_details(session, subject, actor="p" * 64)
        await session.rollback()

        assert await _snapshot(session, _MEMORIES_OUTSIDE, everything) == memories_before
        assert await _snapshot(session, _REVISIONS_OUTSIDE, everything) == revisions_before
        assert (await _live(session, memory_id)).details == SUBJECTS


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
async def test_a_row_not_embedded_yet_is_requeued(
    async_engine: AsyncEngine, world: dict[str, Any], status: str
) -> None:
    """Its point may not exist, and a run in flight may already have read the
    subject's details: no patch, the row goes back to ``pending`` with a fresh
    retry budget, so the next embed writes the payload from the restored row
    (a run in flight loses its claim on the status)."""
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 2, embedding_status=status)
    memory.embedding_retry_count = 3
    memory.embedding_error = "earlier failure" if status == "failed" else None
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            _, report = await _erase_memories(session, subject)
        assert report == RestoreReport(restored=1, embedding_requeued=1, memory_ids=[memory.id])
        live = await _live(session, memory.id)
        assert live.details == AUTHORS
        assert (live.embedding_status, live.embedding_error, live.embedding_retry_count) == (
            "pending",
            None,
            0,
        )
    sync.assert_not_awaited()


async def test_a_strip_by_pseudonym_keeps_a_later_value_over_an_erasure_row(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """#2243 contract: the erasure row carries the pseudonym as actor but put
    back the author's value. A later details edit's prior holds that value,
    so a re-run of the strip by pseudonym must not take it for the subject's."""
    subject, author, third = world["subject"], world["author"], world["third"]
    memory = _memory(world, author, SUBJECTS, 2)
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        with patch(_QDRANT, AsyncMock()):
            pseudonym, _ = await _erase_memories(session, subject)
        # a member edits details after the erasure: the prior is the author's
        session.add(_edit(world, memory, 4, third, {"details": AUTHORS}))
        await session.commit()

        assert await strip_subject_details(session, pseudonym) == StripCounts(0, 0)
        await session.commit()
        assert await _log(session, memory.id) == [
            (memory.id, 2, "api", pseudonym, {"details": AUTHORS}, None),
            (memory.id, 3, "erasure", pseudonym, _put_back(2), None),
            (memory.id, 4, "api", third, {"details": AUTHORS}, None),
        ]


async def test_rows_outside_the_subjects_scope_are_untouched(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """Every memory and revision row the restore has no business with — in
    this context and every other one in the database — is byte-identical
    afterwards — a second tenant with look-alike actors is seeded here, so
    the whole-table snapshot provably covers one. In scope, only the expected
    rows change: the restored memory, the subject's own memory (scrubbed, no
    ``erasure`` row), a memory the subject edited before someone else (not
    restored; the next details edit's prior loses the subject's value, as
    before) and a memory the subject edited without touching ``details``
    (not restored; only their own row is pseudonymized)."""
    subject, author, third = world["subject"], world["author"], world["third"]
    restored = _memory(world, author, SUBJECTS, 3, session_id="sess-s")
    overwritten = _memory(world, author, {"by": "third"}, 3, session_id="sess-t")
    own = _memory(world, subject, SUBJECTS, 2)
    by_third = _memory(world, third, {"u": 3}, 4, session_id="sess-3")
    by_author = _memory(world, author, AUTHORS, 2)
    summary_only = _memory(world, author, {"by": "third"}, 3)
    # a second tenant: its own workspace, look-alike actors, and the same
    # shapes (a newest details edit by a "subject"-like member on top)
    twin_ws, twin_ctx = uuid.uuid4(), uuid.uuid4()
    twin = {**world, "workspace_id": twin_ws, "context_id": twin_ctx}
    twin_subject = f"{subject}-twin"
    twin_memory = _memory(twin, author, SUBJECTS, 3)
    twin_own = _memory(twin, twin_subject, SUBJECTS, 2)
    async with _maker(async_engine)() as session:
        session.add(Workspace(id=twin_ws, name=f"ws-twin-{twin_ws.hex[:8]}", owner_user_id=author))
        await session.flush()
        session.add(Context(id=twin_ctx, workspace_id=twin_ws, name="twin", created_by=author))
        await session.flush()
        await _seed(
            session,
            [twin_memory, twin_own],
            [
                _edit(twin, twin_memory, 2, author, {"details": CREATION}),
                _edit(twin, twin_memory, 3, twin_subject, {"details": AUTHORS}, "sess-s"),
                _edit(twin, twin_own, 2, twin_subject, {"details": CREATION}),
            ],
        )
    try:
        await _assert_scope(
            async_engine,
            world,
            [restored, overwritten, own, by_third, by_author, summary_only],
        )
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(Workspace).where(Workspace.id == twin_ws))
            await session.commit()


async def _assert_scope(
    async_engine: AsyncEngine, world: dict[str, Any], memories: list[Memory]
) -> None:
    subject, author, third, fourth = (
        world["subject"],
        world["author"],
        world["third"],
        world["fourth"],
    )
    restored, overwritten, own, by_third, by_author, summary_only = memories
    async with _maker(async_engine)() as session:
        await _seed(
            session,
            memories,
            [
                _edit(world, summary_only, 2, third, {"details": AUTHORS}, "sess-x"),
                _edit(world, summary_only, 3, subject, {"summary": "a memory"}, "sess-y"),
                _edit(world, restored, 2, author, {"details": CREATION}),
                _edit(world, restored, 3, subject, {"details": AUTHORS}, "sess-s"),
                _edit(world, overwritten, 2, subject, {"details": AUTHORS}, "sess-s2"),
                _edit(world, overwritten, 3, third, {"details": SUBJECTS}, "sess-t"),
                _edit(world, own, 2, subject, {"details": CREATION}),
                _edit(world, by_third, 2, third, {"details": {"u": 1}}, "sess-1"),
                _edit(world, by_third, 3, fourth, {"details": {"u": 2}}, "sess-2"),
                _edit(world, by_third, 4, third, {"details": {"u": 2.5}}, "sess-3"),
                _edit(world, by_author, 2, author, {"details": CREATION}),
            ],
        )
        in_scope = [restored.id, overwritten.id, own.id, summary_only.id]
        revisions_before = await _snapshot(session, _REVISIONS_OUTSIDE, in_scope)
        changed_memories = [restored.id, own.id]
        memories_before = await _snapshot(session, _MEMORIES_OUTSIDE, changed_memories)
        await session.commit()

        sync = AsyncMock()
        with patch(_QDRANT, sync):
            pseudonym, report = await _erase_memories(session, subject)

        assert report.memory_ids == [restored.id]
        assert await _snapshot(session, _REVISIONS_OUTSIDE, in_scope) == revisions_before
        assert await _snapshot(session, _MEMORIES_OUTSIDE, changed_memories) == memories_before
        assert (await _live(session, restored.id)).details == AUTHORS
        assert (await _live(session, own.id)).details is None  # the memory scrub
        assert await _log(session, restored.id, overwritten.id, own.id, summary_only.id) == [
            (restored.id, 2, "api", author, {"details": CREATION}, None),
            (restored.id, 3, "api", pseudonym, {"details": AUTHORS}, None),
            (restored.id, 4, "erasure", pseudonym, _put_back(3), None),
            (overwritten.id, 2, "api", pseudonym, {"details": AUTHORS}, None),
            (overwritten.id, 3, "api", third, {"erased": ["details"]}, "sess-t"),
            (
                own.id,
                2,
                "api",
                pseudonym,
                {"erased": ["details"], "scrubbed": ["details"]},
                None,
            ),
            (summary_only.id, 2, "api", third, {"details": AUTHORS}, "sess-x"),
            (summary_only.id, 3, "api", pseudonym, {"summary": "a memory"}, None),
        ]
    assert [c.kwargs["memory_id"] for c in sync.await_args_list] == [restored.id]


async def test_a_re_run_by_pseudonym_and_a_dry_run_change_nothing(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 2)
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        everything = [uuid.uuid4()]

        # dry run: the counts, no write, no vector-store call
        before = await _snapshot(session, _REVISIONS_OUTSIDE, everything)
        memories_before = await _snapshot(session, _MEMORIES_OUTSIDE, everything)
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            dry = await restore_editor_details(session, subject, actor="p" * 64, dry_run=True)
        await session.flush()
        assert dry == RestoreReport(restored=1, memory_ids=[memory.id])
        assert await _snapshot(session, _REVISIONS_OUTSIDE, everything) == before
        assert await _snapshot(session, _MEMORIES_OUTSIDE, everything) == memories_before
        await session.rollback()
        sync.assert_not_awaited()

        with patch(_QDRANT, AsyncMock()):
            pseudonym, _ = await _erase_memories(session, subject)
        after = await _snapshot(session, _REVISIONS_OUTSIDE, everything)
        memories_after = await _snapshot(session, _MEMORIES_OUTSIDE, everything)
        await session.commit()

        # #2243: the top details edit is the erasure row, not the subject's
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            again = await restore_editor_details(session, pseudonym, actor=pseudonym)
        await session.commit()
        assert again == RestoreReport()
        assert await _snapshot(session, _REVISIONS_OUTSIDE, everything) == after
        assert await _snapshot(session, _MEMORIES_OUTSIDE, everything) == memories_after
    sync.assert_not_awaited()


async def test_a_live_value_equal_to_the_target_is_not_rewritten(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """The live value is already the one someone else wrote (an unlogged
    maintenance write put it back): nothing of the subject's is left on the
    row, so no version and no row."""
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, AUTHORS, 2)
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            report = await restore_editor_details(session, subject, actor="p" * 64)
        await session.commit()
        assert report == RestoreReport()
        live = await _live(session, memory.id)
        assert (live.details, live.version) == (AUTHORS, 2)
        assert len(await _log(session, memory.id)) == 1
    sync.assert_not_awaited()


async def test_the_subjects_rollback_of_another_session_restores_nothing(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """Review M1: T's session edited the author's details and the subject (the
    context owner) rolled it back. The live value is the author's again and
    nothing of the subject's is on the memory: no restore, no row, no patch
    — the rolled-back value does not come back."""
    subject, author, third = world["subject"], world["author"], world["third"]
    memory = _memory(world, author, AUTHORS, 3)
    rollback = _edit(world, memory, 3, subject, {"details": SUBJECTS_2})
    rollback.writer = "rollback"
    undone = _edit(world, memory, 2, third, {"details": AUTHORS}, "sess-t")
    undone.undone_by = 1
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [undone, rollback])
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            pseudonym, report = await _erase_memories(session, subject)
        assert report == RestoreReport()
        live = await _live(session, memory.id)
        assert (live.details, live.version) == (AUTHORS, 3)
        assert [r[1:4] for r in await _log(session, memory.id)] == [
            (2, "api", third),
            (3, "rollback", pseudonym),
        ]
    sync.assert_not_awaited()


async def test_a_second_erasure_finds_the_value_the_first_one_put_back(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """Review M2: A authors (CREATION); V edits (AUTHORS → V's value); U edits
    over it. U's erasure puts V's value back; V's later erasure must find
    that value is V's and put the author's back. A re-run by either
    pseudonym afterwards (#2243) changes nothing."""
    first, second, author = world["subject"], world["third"], world["author"]
    memory = _memory(world, author, SUBJECTS, 3)
    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [memory],
            [
                _edit(world, memory, 2, second, {"details": AUTHORS}),
                _edit(world, memory, 3, first, {"details": SUBJECTS_2}),
            ],
        )
        with patch(_QDRANT, AsyncMock()):
            p_first, first_report = await _erase_memories(session, first)
        assert (await _live(session, memory.id)).details == SUBJECTS_2  # V's value
        with patch(_QDRANT, AsyncMock()):
            p_second, second_report = await _erase_memories(session, second)

        assert (first_report.restored, second_report.restored) == (1, 1)
        live = await _live(session, memory.id)
        assert (live.details, live.version) == (AUTHORS, 5)
        assert await _log(session, memory.id) == [
            (memory.id, 2, "api", p_second, {"details": AUTHORS}, None),
            # V's value, stripped by V's erasure
            (memory.id, 3, "api", p_first, {"erased": ["details"]}, None),
            (memory.id, 4, "erasure", p_first, _put_back(3), None),
            (memory.id, 5, "erasure", p_second, _put_back(2), None),
        ]
        log = await _log(session, memory.id)
        await session.commit()

        sync = AsyncMock()
        with patch(_QDRANT, sync):
            for pseudonym in (p_second, p_first):
                assert await restore_editor_details(session, pseudonym, actor=pseudonym) == (
                    RestoreReport()
                )
        await session.commit()
        assert await _log(session, memory.id) == log
    sync.assert_not_awaited()


async def test_a_value_the_subjects_rollback_put_back_keeps_its_prior(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """Review B3 (#2243): T's session edited the author's details and the
    subject (the context owner) rolled it back, putting the author's value
    back; a fourth member then edited over it. That edit's prior is the
    author's value, not the subject's — the strip keeps it, during erasure
    and on a re-run by pseudonym. The rollback's own prior (T's undone value)
    stays too."""
    subject, author = world["subject"], world["author"]
    third, fourth = world["third"], world["fourth"]
    memory = _memory(world, author, {"by": "fourth"}, 4)
    undone = _edit(world, memory, 2, third, {"details": AUTHORS}, "sess-t")
    undone.undone_by = 1
    rollback = _edit(world, memory, 3, subject, {"details": SUBJECTS_2})
    rollback.writer = "rollback"
    later = _edit(world, memory, 4, fourth, {"details": AUTHORS}, "sess-4")
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [undone, rollback, later])
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            pseudonym, report = await _erase_memories(session, subject)
        assert report == RestoreReport()
        assert await _log(session, memory.id) == [
            (memory.id, 2, "api", third, {"details": AUTHORS}, "sess-t"),
            (memory.id, 3, "rollback", pseudonym, {"details": SUBJECTS_2}, None),
            # the author's value the rollback put back: kept
            (memory.id, 4, "api", fourth, {"details": AUTHORS}, "sess-4"),
        ]
        live = await _live(session, memory.id)
        assert (live.details, live.version) == ({"by": "fourth"}, 4)
        assert (await load_version(session, live, 3)).values["details"] == AUTHORS

        assert await strip_subject_details(session, pseudonym) == StripCounts(0, 0)
        await session.commit()
    sync.assert_not_awaited()
