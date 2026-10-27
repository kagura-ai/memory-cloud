"""#2244: account erasure puts back the details an erased editor wrote.

Against a real Postgres. Where the erased subject made the newest ``details``
edit of a memory another member authored, the live ``details`` goes back to
the last value someone else wrote, with a version bump and one ``erasure``
revision row; the vector payload's ``location`` follows after the commit
(``sync_restored_payloads``, #2266).

1. the restore, then the rest of the erasure's memory steps, on the author's
   memory: live value, version, last writer, the new row, the rebuilt
   versions, the payload patch (``location`` set);
2. the subject's consecutive edits are walked past (and the strip still
   removes their earlier value from the log);
3. a double erasure: the value to go back to was removed earlier → NULL,
   ``location`` cleared from the payload;
4. a tombstone is restored in Postgres with no payload call;
5. a payload failure after the commit re-queues the embedding and keeps the
   restore; a row not embedded yet is re-queued and still patched; a row
   edited between the commit and the patch is not patched;
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
    PayloadSync,
    RestoreReport,
    StripCounts,
    erase_subject_details,
    lock_subject_memories,
    restore_editor_details,
    strip_subject_details,
    sync_restored_payloads,
)
from services.memory_versions import load_version
from utils.datetime import utcnow
from utils.exceptions import QdrantError
from utils.time_trigger import normalize_trigger

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
    kind: str = "note",
) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=owner,
        workspace_id=world["workspace_id"],
        context_id=world["context_id"],
        summary="a memory",
        content="body",
        type=kind,
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


def _put_back(restored_from: int, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """The prior of the erasure row (#2275): the overwritten value minus the
    subject's keys, and the lowest version a value it reverted was written at."""
    return {"details": details or {}, "restored_from": restored_from}


def _service(session: AsyncSession) -> AccountErasureService:
    service = AccountErasureService.__new__(AccountErasureService)
    service.db = session
    return service


async def _erase_memories(session: AsyncSession, subject: str) -> tuple[str, RestoreReport]:
    """The memory steps of ``_delete_postgres``, in its order, then commit,
    then the payload step ``_execute`` runs after it."""
    service = _service(session)
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        pseudonym = service._pseudonym(subject)
        await lock_subject_memories(session, subject)
        await service._delete_subject_tombstones(subject)
        report = await restore_editor_details(session, subject, actor=pseudonym)
        await service._erase_session_provenance(subject)
        await service._erase_memory_revisions(subject)
        await service._scrub_surviving_memories(subject)
    await session.commit()
    await sync_restored_payloads(session, report.to_sync, report)
    return pseudonym, report


def _payload(report: RestoreReport) -> tuple[int, ...]:
    """``(synced, tombstone, shared, moved, skipped_requeued, requeued_after_error)``."""
    return (
        report.payload_synced,
        report.payload_skipped_tombstone,
        report.payload_skipped_shared,
        report.payload_skipped_moved,
        report.payload_skipped_requeued,
        report.payload_requeued_after_error,
    )


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


async def _outside(session: AsyncSession, changed: list[uuid.UUID]) -> tuple[dict, dict]:
    """Byte-level snapshots of every memory and revision row outside ``changed``
    (committed, so the erasure's own transaction starts after it)."""
    snapshots = (
        await _snapshot(session, _MEMORIES_OUTSIDE, changed),
        await _snapshot(session, _REVISIONS_OUTSIDE, changed),
    )
    await session.commit()
    return snapshots


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

        assert (report.restored, report.unavailable, report.memory_ids) == (1, 0, [memory.id])
        assert report.to_sync == [PayloadSync(memory.id, 5)]
        assert _payload(report) == (1, 0, 0, 0, 0, 0)
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
        # the subject's version reads without the subject's keys (#2275); the
        # author's reads back
        at_4 = await load_version(session, live, 4)
        assert at_4.unavailable == () and at_4.values["details"] == {}
        assert (await load_version(session, live, 3)).values["details"] == AUTHORS
        assert (await load_version(session, live, 1)).values["details"] == CREATION

    sync.assert_awaited_once()
    kwargs = sync.await_args.kwargs
    assert kwargs["memory_id"] == memory.id
    assert kwargs["payload_updates"]["location"] == {"lat": 10.5, "lon": 20.25}
    assert set(kwargs["payload_updates"]) == {"location", "updated_at"}
    # The restored details carry no observed_at, so the key is cleared (#2330).
    assert kwargs["delete_keys"] == ["observed_at"]
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

        assert (report.restored, report.unavailable, report.keys_reverted) == (1, 0, 2)
        # #2275: ``by`` goes back past both of the subject's edits; their
        # ``secret`` goes; the author's ``location`` they removed stays removed
        assert (await _live(session, memory.id)).details == {"by": "author"}
        assert await _log(session, memory.id) == [
            (memory.id, 2, "api", author, {"details": CREATION}, None),
            (memory.id, 3, "api", pseudonym, {"details": AUTHORS}, None),
            (memory.id, 4, "api", pseudonym, {"summary": "not details"}, None),
            # the subject's first value: both keys are theirs, stripped
            (memory.id, 5, "api", pseudonym, {"details": {}}, None),
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
    assert kwargs["delete_keys"] == ["location", "observed_at"]  # #2330


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

        assert (report.restored, report.embedding_requeued) == (1, 0)
        assert _payload(report) == (0, 1, 0, 0, 0, 0)
        live = await _live(session, memory.id)
        assert (live.details, live.version) == (AUTHORS, 3)
        assert live.deleted_at is not None
        assert (await _log(session, memory.id))[-1][1:4] == (3, "erasure", pseudonym)
    sync.assert_not_awaited()


async def test_the_subjects_tombstones_leave_before_the_strip(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """#2263: the subject's own tombstone is hard-deleted right after the
    memory lock, so the strip and the scrub mark never lock or count its
    revision rows (the FK cascade takes them). The counts are exactly those
    of the same scenario without the tombstone: one stripped row on the
    subject's live memory, one on a tombstone the subject only edited (kept,
    stripped as before), one scrub mark. Rows of no memory in scope change."""
    subject, author, third = world["subject"], world["author"], world["third"]
    tombstone = _memory(world, subject, SUBJECTS, 3, deleted=True)
    live = _memory(world, subject, SUBJECTS, 2)
    edited = _memory(world, author, {"by": "third"}, 3, deleted=True)
    in_scope = [tombstone.id, live.id, edited.id]
    strips: list[StripCounts] = []

    async def strip(*args: Any, **kwargs: Any) -> StripCounts:
        strips.append(await strip_subject_details(*args, **kwargs))
        return strips[-1]

    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [tombstone, live, edited],
            [
                _edit(world, tombstone, 2, third, {"details": CREATION}),
                _edit(world, tombstone, 3, author, {"details": AUTHORS}),
                _edit(world, live, 2, third, {"details": CREATION}),
                _edit(world, edited, 2, subject, {"details": AUTHORS}),
                _edit(world, edited, 3, third, {"details": SUBJECTS}),
            ],
        )
        revisions_before = await _snapshot(session, _REVISIONS_OUTSIDE, in_scope)
        memories_before = await _snapshot(session, _MEMORIES_OUTSIDE, in_scope)
        await session.commit()

        service = _service(session)
        with (
            patch(_QDRANT, AsyncMock()),
            patch("services.account_erasure_service._audit_salt", return_value="salt"),
            patch("services.account_erasure_service.strip_subject_details", new=strip),
        ):
            pseudonym = service._pseudonym(subject)
            await lock_subject_memories(session, subject)
            assert await service._delete_subject_tombstones(subject) == 1
            await restore_editor_details(session, subject, actor=pseudonym)
            await service._erase_session_provenance(subject)
            counts = await service._erase_memory_revisions(subject)
            assert await service._scrub_surviving_memories(subject) == 1  # ``live``
        await session.commit()

        assert strips == [StripCounts(details_stripped=2, scrub_marked=1)]
        assert counts["revision_details_stripped"] == 2
        remaining = (
            await session.execute(select(Memory.id).where(Memory.id.in_(in_scope)))
        ).scalars()
        assert set(remaining) == {live.id, edited.id}
        assert await _log(session, tombstone.id, live.id, edited.id) == [
            (live.id, 2, "api", third, {"erased": ["details"], "scrubbed": ["details"]}, None),
            (edited.id, 2, "api", pseudonym, {"details": AUTHORS}, None),
            # #2275: both keys of the subject's value leave the prior
            (edited.id, 3, "api", third, {"details": {}}, None),
        ]
        assert (await _live(session, edited.id)).details == {"by": "third"}
        assert await _snapshot(session, _REVISIONS_OUTSIDE, in_scope) == revisions_before
        assert await _snapshot(session, _MEMORIES_OUTSIDE, in_scope) == memories_before


async def test_a_payload_failure_requeues_the_embedding_and_keeps_the_restore(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """#2266: the patch runs after the commit; when it fails, the embedded
    row is re-queued (the sweep's upsert rewrites the payload from the
    restored row) and the restore stays."""
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 2)
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        others = await _snapshot(session, _MEMORIES_OUTSIDE, [memory.id])
        await session.commit()
        with patch(_QDRANT, AsyncMock(side_effect=QdrantError("vector store down"))):
            pseudonym, report = await _erase_memories(session, subject)

        assert _payload(report) == (0, 0, 0, 0, 0, 1)
        live = await _live(session, memory.id)
        assert (live.details, live.version, live.embedding_status) == (AUTHORS, 3, "pending")
        assert (await _log(session, memory.id))[-1][1:4] == (3, "erasure", pseudonym)
        assert await _snapshot(session, _MEMORIES_OUTSIDE, [memory.id]) == others


async def test_a_payload_patch_runs_after_the_commit(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """The restored value is committed (visible to another session) when the
    vector store is called: no vector-store call while the locks are held."""
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 2)
    seen: list[Any] = []

    async def patch_payload(**_: Any) -> None:
        async with _maker(async_engine)() as other:
            seen.append((await _live(other, memory.id)).details)

    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        before = await _outside(session, [memory.id])
        with patch(_QDRANT, AsyncMock(side_effect=patch_payload)):
            _, report = await _erase_memories(session, subject)
        assert await _outside(session, [memory.id]) == before
    assert seen == [AUTHORS]
    assert report.payload_synced == 1


async def test_a_row_edited_after_the_restore_is_not_patched(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """A member edits the restored memory between the commit and the patch:
    that edit synced its own payload, so the restore's is skipped."""
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 2)
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        service = _service(session)
        with patch("services.account_erasure_service._audit_salt", return_value="salt"):
            report = await restore_editor_details(
                session, subject, actor=service._pseudonym(subject)
            )
        await session.commit()
        async with _maker(async_engine)() as other:
            await other.execute(
                text("UPDATE memories SET version = version + 1 WHERE id = :id"),
                {"id": memory.id},
            )
            await other.commit()
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            await sync_restored_payloads(session, report.to_sync, report)
    assert _payload(report) == (0, 0, 0, 1, 0, 0)
    sync.assert_not_awaited()


async def test_a_shared_point_is_counted_apart_from_a_tombstone(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    subject, author = world["subject"], world["author"]
    shared = _memory(world, author, SUBJECTS, 2)
    tombstone = _memory(world, author, SUBJECTS, 2, deleted=True)
    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [shared, tombstone],
            [
                _edit(world, shared, 2, subject, {"details": AUTHORS}),
                _edit(world, tombstone, 2, subject, {"details": AUTHORS}),
            ],
        )
        before = await _outside(session, [shared.id, tombstone.id])
        sync = AsyncMock()
        with (
            patch(_QDRANT, sync),
            patch("services.resource_indexer.unshared_point_id", new=AsyncMock(return_value=None)),
        ):
            _, report = await _erase_memories(session, subject)
        assert await _outside(session, [shared.id, tombstone.id]) == before
    assert report.restored == 2
    assert _payload(report) == (0, 1, 1, 0, 0, 0)
    sync.assert_not_awaited()


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
async def test_a_row_not_embedded_yet_is_requeued(
    async_engine: AsyncEngine, world: dict[str, Any], status: str
) -> None:
    """A run in flight may already have read the subject's details: the row
    goes back to ``pending`` with a fresh retry budget in the restore's
    transaction, so the next embed writes the payload from the restored row
    (a run in flight loses its claim on the status). Its point may exist
    still, with the subject's location: the patch is attempted too (#2266);
    a failure there leaves it to the re-embed."""
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 2, embedding_status=status)
    memory.embedding_retry_count = 3
    memory.embedding_error = "earlier failure" if status == "failed" else None
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            _, report = await _erase_memories(session, subject)
        assert (report.restored, report.embedding_requeued) == (1, 1)
        assert _payload(report) == (1, 0, 0, 0, 0, 0)
        live = await _live(session, memory.id)
        assert live.details == AUTHORS
        assert (live.embedding_status, live.embedding_error, live.embedding_retry_count) == (
            "pending",
            None,
            0,
        )
    sync.assert_awaited_once()


@pytest.mark.parametrize("status", ["pending", "failed"])
async def test_a_failed_patch_on_a_requeued_row_is_left_to_the_re_embed(
    async_engine: AsyncEngine, world: dict[str, Any], status: str
) -> None:
    subject, author = world["subject"], world["author"]
    memory = _memory(world, author, SUBJECTS, 2, embedding_status=status)
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        before = await _outside(session, [memory.id])
        with patch(_QDRANT, AsyncMock(side_effect=QdrantError("no such point"))):
            _, report = await _erase_memories(session, subject)
        assert _payload(report) == (0, 0, 0, 0, 1, 0)
        assert (await _live(session, memory.id)).embedding_status == "pending"
        assert await _outside(session, [memory.id]) == before


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
            (overwritten.id, 3, "api", third, {"details": {}}, "sess-t"),
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
        # no to_sync
        assert dry == RestoreReport(restored=1, keys_reverted=2, memory_ids=[memory.id])
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
        # V's ``by`` is back; U's ``location`` goes; V's ``secret`` that U
        # removed stays removed (#2275: deletions stand)
        assert (await _live(session, memory.id)).details == {"by": "subject-again"}
        with patch(_QDRANT, AsyncMock()):
            p_second, second_report = await _erase_memories(session, second)

        assert (first_report.restored, second_report.restored) == (1, 1)
        live = await _live(session, memory.id)
        # the author's ``by``; the ``location`` V removed stays removed
        assert (live.details, live.version) == ({"by": "author"}, 5)
        assert await _log(session, memory.id) == [
            (memory.id, 2, "api", p_second, {"details": AUTHORS}, None),
            # V's value, stripped by V's erasure
            (memory.id, 3, "api", p_first, {"details": {}}, None),
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


# ---------------------------------------------------------------------------
# #2266: time memories keep a trigger window (valid_trigger_window_format)
# ---------------------------------------------------------------------------

TRIGGER = normalize_trigger({"year": 2027, "month": 1, "day": 1})
AUTHORS_TRIGGER = normalize_trigger({"year": 2028, "month": 3})


@pytest.mark.parametrize(
    ("prior", "expected", "counts"),
    [
        # the value to go back to was erased earlier: only the live trigger
        ({"erased": ["details"]}, {"trigger": TRIGGER}, (0, 1)),
        # the subject made the row a time memory in the same edit: the author's
        # value has no trigger, so the live one is merged in
        ({"details": AUTHORS}, {**AUTHORS, "trigger": TRIGGER}, (1, 0)),
        ({"details": None}, {"trigger": TRIGGER}, (1, 0)),
    ],
)
async def test_a_time_memory_keeps_the_live_trigger(
    async_engine: AsyncEngine,
    world: dict[str, Any],
    prior: dict[str, Any],
    expected: dict[str, Any],
    counts: tuple[int, int],
) -> None:
    subject, author, third = world["subject"], world["author"], world["third"]
    memory = _memory(world, author, {**SUBJECTS, "trigger": TRIGGER}, 2, kind="time")
    # another author's time memory the subject never touched
    bystander = _memory(world, third, {"trigger": AUTHORS_TRIGGER}, 1, kind="time")
    async with _maker(async_engine)() as session:
        await _seed(session, [memory, bystander], [_edit(world, memory, 2, subject, prior)])
        everything_else = [memory.id]
        revisions_before = await _snapshot(session, _REVISIONS_OUTSIDE, everything_else)
        memories_before = await _snapshot(session, _MEMORIES_OUTSIDE, everything_else)
        await session.commit()
        with patch(_QDRANT, AsyncMock()):
            pseudonym, report = await _erase_memories(session, subject)

        assert (report.restored, report.unavailable) == counts
        live = await _live(session, memory.id)
        assert (live.details, live.version) == (expected, 3)
        assert (live.trigger_from, live.trigger_until) == (TRIGGER["from"], TRIGGER["until"])
        # the subject's coordinates are gone (the author's, if any, are back)
        assert live.location_lat == (10.5 if "location" in expected else None)
        assert (await _log(session, memory.id))[-1] == (
            memory.id,
            3,
            "erasure",
            pseudonym,
            _put_back(2),
            None,
        )
        assert await _snapshot(session, _REVISIONS_OUTSIDE, everything_else) == revisions_before
        assert await _snapshot(session, _MEMORIES_OUTSIDE, everything_else) == memories_before


async def test_a_merged_live_trigger_loses_its_text(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """The subject may have written the live trigger: only its window is
    merged into the author's value, never its ``text``."""
    subject, author = world["subject"], world["author"]
    labelled = normalize_trigger({"year": 2027, "month": 1, "day": 1, "text": "private"})
    # the subject added the trigger and carried the author's keys
    memory = _memory(world, author, {**AUTHORS, "trigger": labelled}, 2, kind="time")
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": AUTHORS})])
        before = await _outside(session, [memory.id])
        with patch(_QDRANT, AsyncMock()):
            await _erase_memories(session, subject)
        live = await _live(session, memory.id)
        assert live.details == {**AUTHORS, "trigger": TRIGGER}
        assert "private" not in repr(live.details)
        assert await _outside(session, [memory.id]) == before


# A trigger the author wrote while the row was a note: never normalized.
UNNORMALIZED = [
    # components only, no window: normalized
    ({"year": 2028, "month": 3}, AUTHORS_TRIGGER),
    # a date-only window (from/until strings the CHECK's regex rejects) and no
    # components: cannot be normalized, so the live window goes in instead
    ({"from": "2027-01-01", "until": "2027-01-31"}, TRIGGER),
]


@pytest.mark.parametrize(("raw_trigger", "expected"), UNNORMALIZED)
async def test_an_unnormalized_authors_trigger_is_normalized(
    async_engine: AsyncEngine,
    world: dict[str, Any],
    raw_trigger: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """The subject made the author's note a time memory. Put back as is, the
    author's trigger would break the CHECK (and fail the erasure after the
    vectors are gone): it is normalized, or replaced by the live window."""
    subject, author = world["subject"], world["author"]
    raw = {"trigger": raw_trigger, "by": "author"}
    # the subject rewrote the trigger (with a text of theirs) and carried the
    # author's ``by``
    subjects = normalize_trigger({"year": 2027, "month": 1, "day": 1, "text": "subject's"})
    memory = _memory(world, author, {"trigger": subjects, "by": "author"}, 2, kind="time")
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": raw})])
        before = await _outside(session, [memory.id])
        with patch(_QDRANT, AsyncMock()):
            _, report = await _erase_memories(session, subject)
        assert report.restored == 1
        live = await _live(session, memory.id)
        assert live.details == {"by": "author", "trigger": expected}
        assert (live.trigger_from, live.trigger_until) == (expected["from"], expected["until"])
        assert await _outside(session, [memory.id]) == before


async def test_a_time_memory_gets_the_authors_trigger_back(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """The author's value has a trigger of its own: restored verbatim."""
    subject, author = world["subject"], world["author"]
    authors = {"trigger": AUTHORS_TRIGGER, "by": "author"}
    memory = _memory(world, author, {**SUBJECTS, "trigger": TRIGGER}, 2, kind="time")
    async with _maker(async_engine)() as session:
        await _seed(session, [memory], [_edit(world, memory, 2, subject, {"details": authors})])
        before = await _outside(session, [memory.id])
        with patch(_QDRANT, AsyncMock()):
            _, report = await _erase_memories(session, subject)
        assert (report.restored, report.unavailable) == (1, 0)
        live = await _live(session, memory.id)
        assert live.details == authors
        assert live.trigger_from == AUTHORS_TRIGGER["from"]
        assert await _outside(session, [memory.id]) == before


async def test_the_subjects_own_time_memory_keeps_only_its_trigger(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    subject, author = world["subject"], world["author"]
    own = _memory(world, subject, {**SUBJECTS, "trigger": TRIGGER}, 1, kind="time")
    own_note = _memory(world, subject, SUBJECTS, 1)
    others = _memory(world, author, {**AUTHORS, "trigger": AUTHORS_TRIGGER}, 1, kind="time")
    async with _maker(async_engine)() as session:
        await _seed(session, [own, own_note, others], [])
        memories_before = await _snapshot(session, _MEMORIES_OUTSIDE, [own.id, own_note.id])
        await session.commit()
        with patch(_QDRANT, AsyncMock()):
            pseudonym, _ = await _erase_memories(session, subject)
        live = await _live(session, own.id)
        assert (live.user_id, live.details) == (pseudonym, {"trigger": TRIGGER})
        assert (live.trigger_from, live.location_lat) == (TRIGGER["from"], None)
        assert (await _live(session, own_note.id)).details is None
        assert await _snapshot(session, _MEMORIES_OUTSIDE, [own.id, own_note.id]) == (
            memories_before
        )


# ---------------------------------------------------------------------------
# #2266 review: the post-commit payload step never fails the request
# ---------------------------------------------------------------------------


async def test_a_non_vector_store_error_requeues_the_row_and_the_next_one_syncs(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """A read between the patch target and the call fails for the first row
    (not a ``QdrantError``): that row is re-queued, the second is patched."""
    subject, author = world["subject"], world["author"]
    first, second = sorted(
        (_memory(world, author, SUBJECTS, 2) for _ in range(2)), key=lambda m: m.id
    )
    from services.context_routing import resolve_collection_name as real

    calls = 0

    async def flaky(db: AsyncSession, context_id: uuid.UUID) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("context read failed")
        return await real(db, context_id)

    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [first, second],
            [
                _edit(world, first, 2, subject, {"details": AUTHORS}),
                _edit(world, second, 2, subject, {"details": AUTHORS}),
            ],
        )
        before = await _outside(session, [first.id, second.id])
        sync = AsyncMock()
        with (
            patch(_QDRANT, sync),
            patch("services.context_routing.resolve_collection_name", new=flaky),
        ):
            _, report = await _erase_memories(session, subject)
        assert _payload(report) == (1, 0, 0, 0, 0, 1)
        assert (await _live(session, first.id)).embedding_status == "pending"
        assert (await _live(session, second.id)).embedding_status == "success"
        assert (await _live(session, first.id)).details == AUTHORS  # the restore stays
        assert await _outside(session, [first.id, second.id]) == before
    assert [c.kwargs["memory_id"] for c in sync.await_args_list] == [second.id]


async def test_a_failed_requeue_is_counted_and_the_next_row_still_syncs(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    subject, author = world["subject"], world["author"]
    first, second = sorted(
        (_memory(world, author, SUBJECTS, 2) for _ in range(2)), key=lambda m: m.id
    )
    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [first, second],
            [
                _edit(world, first, 2, subject, {"details": AUTHORS}),
                _edit(world, second, 2, subject, {"details": AUTHORS}),
            ],
        )
        sync = AsyncMock(side_effect=[QdrantError("down"), None])
        with (
            patch(_QDRANT, sync),
            patch(
                "services.content_chunk_writer.requeue_embedding",
                new=AsyncMock(side_effect=RuntimeError("requeue failed")),
            ),
        ):
            _, report = await _erase_memories(session, subject)
        assert (report.payload_synced, report.payload_requeue_failed) == (1, 1)
        assert (await _live(session, second.id)).details == AUTHORS


async def test_payload_groups_commit_one_by_one(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """With groups of one, the first row's share lock is released before the
    second row's patch: another writer can lock it without waiting."""
    subject, author = world["subject"], world["author"]
    first, second = sorted(
        (_memory(world, author, SUBJECTS, 2) for _ in range(2)), key=lambda m: m.id
    )
    free: list[bool] = []

    async def patch_payload(**kwargs: Any) -> None:
        if kwargs["memory_id"] != second.id:
            return
        async with _maker(async_engine)() as other:
            await other.execute(text("SET LOCAL lock_timeout = '500ms'"))
            row = await other.execute(
                text("SELECT id FROM memories WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": first.id},
            )
            free.append(row.scalar_one() == first.id)
            await other.rollback()

    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [first, second],
            [
                _edit(world, first, 2, subject, {"details": AUTHORS}),
                _edit(world, second, 2, subject, {"details": AUTHORS}),
            ],
        )
        with (
            patch(_QDRANT, AsyncMock(side_effect=patch_payload)),
            patch("services.payload_sync.PAYLOAD_SYNC_GROUP", 1),
        ):
            _, report = await _erase_memories(session, subject)
    assert report.payload_synced == 2
    assert free == [True]


# ---------------------------------------------------------------------------
# #2275: per-key provenance — a later whole-dict edit carries the subject's keys
# ---------------------------------------------------------------------------

L_S = {"lat": 35.0, "lon": 139.0}  # the subject's location
L_A = {"lat": 10.5, "lon": 20.25}  # the author's


def _created(world: dict[str, Any], memory: Memory, actor: str) -> MemoryRevision:
    row = _edit(world, memory, 1, actor, {})
    row.kind = "created"
    return row


async def _seed_worked_example(
    session: AsyncSession, world: dict[str, Any]
) -> tuple[Memory, list[Memory]]:
    """gate1's table: AUTH {a:1}; S adds location and t; B rewrites a; C
    rewrites t and adds c (each a whole-dict write carrying the rest). And
    the bystanders whose rows must not move: another memory of the author's
    with the same key names, a memory only B edited, a survivor's time
    memory, and a second tenant's look-alike."""
    subject, author, b, c = world["subject"], world["author"], world["third"], world["fourth"]
    memory = _memory(world, author, {"a": 2, "location": L_S, "t": "z", "c": True}, 4)
    same_keys = _memory(world, author, {"a": 2, "location": L_A, "t": "z"}, 2)
    only_b = _memory(world, author, {"a": 3}, 2)
    survivors_time = _memory(world, b, {"trigger": TRIGGER, "t": "y"}, 2, kind="time")
    others = [same_keys, only_b, survivors_time]
    await _seed(
        session,
        [memory, *others],
        [
            _created(world, memory, author),
            _edit(world, memory, 2, subject, {"details": {"a": 1}}),
            _edit(world, memory, 3, b, {"details": {"a": 1, "location": L_S, "t": "y"}}),
            _edit(world, memory, 4, c, {"details": {"a": 2, "location": L_S, "t": "y"}}),
            _edit(world, same_keys, 2, author, {"details": {"a": 1, "location": L_A, "t": "y"}}),
            _edit(world, only_b, 2, b, {"details": {"a": 1, "location": L_S}}),
            _edit(world, survivors_time, 2, b, {"details": {"trigger": TRIGGER, "t": "x"}}),
        ],
    )
    return memory, others


async def test_a_carried_key_is_reverted_and_stripped_per_key(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """#2275, the issue scenario: the subject's ``location`` survived B's and
    C's whole-dict edits. It leaves the live value, both priors that carried
    it and the payload; B's ``a`` and C's ``t`` / ``c`` stay, in the live row
    and in every prior; the author's rows and every row outside the memory
    are byte-identical."""
    subject, author, b, c = world["subject"], world["author"], world["third"], world["fourth"]
    async with _maker(async_engine)() as session:
        memory, others = await _seed_worked_example(session, world)
        before = await _outside(session, [memory.id])
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            pseudonym, report = await _erase_memories(session, subject)

        assert (report.restored, report.unavailable, report.keys_reverted) == (1, 0, 1)
        assert report.keys_ambiguous == 0
        live = await _live(session, memory.id)
        assert (live.details, live.version) == ({"a": 2, "t": "z", "c": True}, 5)
        assert await _log(session, memory.id) == [
            (memory.id, 1, "api", author, {}, None),
            (memory.id, 2, "api", pseudonym, {"details": {"a": 1}}, None),
            # S's location and t (carried by B) leave B's and C's priors
            (memory.id, 3, "api", b, {"details": {"a": 1}}, None),
            (memory.id, 4, "api", c, {"details": {"a": 2}}, None),
            (memory.id, 5, "erasure", pseudonym, _put_back(2, live.details), None),
        ]
        assert (await load_version(session, live, 4)).values["details"] == live.details
        assert (await load_version(session, live, 3)).values["details"] == {"a": 2}
        assert (await load_version(session, live, 2)).values["details"] == {"a": 1}
        # nothing outside the memory moved
        assert await _outside(session, [memory.id]) == before
    sync.assert_awaited_once()
    assert sync.await_args.kwargs["memory_id"] == memory.id
    assert "location" in sync.await_args.kwargs["delete_keys"]


async def test_a_carried_key_goes_back_to_the_authors_value(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    subject, author, b = world["subject"], world["author"], world["third"]
    memory = _memory(world, author, {"a": 3, "location": L_S}, 3)
    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [memory],
            [
                _edit(world, memory, 2, subject, {"details": {"a": 1, "location": L_A}}),
                _edit(world, memory, 3, b, {"details": {"a": 1, "location": L_S}}),
            ],
        )
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            pseudonym, report = await _erase_memories(session, subject)
        assert (report.restored, report.keys_reverted) == (1, 1)
        assert (await _live(session, memory.id)).details == {"a": 3, "location": L_A}
        assert (await _log(session, memory.id))[1:] == [
            (memory.id, 3, "api", b, {"details": {"a": 1}}, None),
            (memory.id, 4, "erasure", pseudonym, _put_back(2, {"a": 3}), None),
        ]
    assert sync.await_args.kwargs["payload_updates"]["location"] == L_A


async def test_a_second_erasure_reverts_only_its_own_keys(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """S's erasure, then B's: B's ``a`` goes back to the author's; C's keys
    stay; a re-run by either pseudonym and a dry run change nothing."""
    subject, author, b, c = world["subject"], world["author"], world["third"], world["fourth"]
    async with _maker(async_engine)() as session:
        memory, _ = await _seed_worked_example(session, world)
        with patch(_QDRANT, AsyncMock()):
            p_s, _ = await _erase_memories(session, subject)
            p_b, report = await _erase_memories(session, b)
        # this memory's ``a``, and the ``a`` B wrote on ``only_b``
        assert (report.restored, report.keys_reverted) == (2, 2)
        live = await _live(session, memory.id)
        assert (live.details, live.version) == ({"a": 1, "t": "z", "c": True}, 6)
        assert await _log(session, memory.id) == [
            (memory.id, 1, "api", author, {}, None),
            (memory.id, 2, "api", p_s, {"details": {"a": 1}}, None),
            (memory.id, 3, "api", p_b, {"details": {"a": 1}}, None),
            (memory.id, 4, "api", c, {"details": {}}, None),
            (memory.id, 5, "erasure", p_s, _put_back(2, {"t": "z", "c": True}), None),
            (memory.id, 6, "erasure", p_b, _put_back(3, {"t": "z", "c": True}), None),
        ]
        everything = [uuid.uuid4()]
        after = await _outside(session, everything)
        sync = AsyncMock()
        with patch(_QDRANT, sync):
            for pseudonym, others in ((p_s, {p_b}), (p_b, {p_s})):
                for dry_run in (True, False):
                    again = await erase_subject_details(
                        session,
                        pseudonym,
                        actor=pseudonym,
                        erased_actors=others,
                        dry_run=dry_run,
                    )
                    assert again.restore == RestoreReport()
                    assert again.strip == StripCounts(0, 0)
        await session.commit()
        assert await _outside(session, everything) == after
    sync.assert_not_awaited()


async def test_the_dry_run_counts_per_key_and_writes_nothing(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    async with _maker(async_engine)() as session:
        memory, _ = await _seed_worked_example(session, world)
        everything = [uuid.uuid4()]
        before = await _outside(session, everything)
        dry = await erase_subject_details(
            session, world["subject"], actor="p" * 64, erased_actors=frozenset(), dry_run=True
        )
        await session.flush()
        assert (dry.restore.restored, dry.restore.keys_reverted, dry.restore.memory_ids) == (
            1,
            1,
            [memory.id],
        )
        assert dry.restore.to_sync == []
        # B's and C's priors; the author's memories have no row of the subject's
        assert dry.strip == StripCounts(details_stripped=2, scrub_marked=0)
        assert await _outside(session, everything) == before


async def test_a_pre_v0129_stripped_state_is_counted_ambiguous_and_kept(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """A re-run by the pseudonym over an erasure from before v0.129.0: the
    member's own written state was stripped whole, so a key B's later edit
    shows may be theirs or B's — kept and counted, nothing written. Next to
    it an old-style erasure row on top: the author's value, nothing to do."""
    author, b, p = world["author"], world["third"], "5" * 64
    ambiguous = _memory(world, author, {"a": 1, "s": "x"}, 3)
    old_row = _memory(world, author, {"a": 1}, 3)
    put_back = _edit(world, old_row, 3, p, {"erased": ["details"], "restored_from": 2})
    put_back.writer = "erasure"
    async with _maker(async_engine)() as session:
        await _seed(
            session,
            [ambiguous, old_row],
            [
                _edit(world, ambiguous, 2, p, {"details": {"a": 1}}),
                _edit(world, ambiguous, 3, b, {"erased": ["details"]}),
                _edit(world, old_row, 2, p, {"details": {"a": 1}}),
                put_back,
            ],
        )
        everything = [uuid.uuid4()]
        before = await _outside(session, everything)
        for dry_run in (True, False):
            with patch(_QDRANT, AsyncMock()) as sync:
                result = await erase_subject_details(
                    session, p, actor=p, erased_actors=frozenset(), dry_run=dry_run
                )
            await session.commit()
            sync.assert_not_awaited()
            assert result.restore == RestoreReport(keys_ambiguous=1)
            assert result.strip == StripCounts(0, 0)
            assert await _outside(session, everything) == before
