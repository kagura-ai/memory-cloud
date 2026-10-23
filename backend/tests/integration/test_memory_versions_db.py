"""Memory versions against a real Postgres (#2043).

1. Two concurrent ``update_memory`` calls with the same ``expected_version``:
   exactly one applies, the other gets ``version_conflict`` with the new
   version — the ``SELECT ... FOR UPDATE`` re-read makes the second edit wait
   for the first and compare against its result.
2. Two concurrent edits without ``expected_version`` both apply and the
   version moves twice (no lost increment), one ``updated`` row each.
3. ``memory_revisions`` rows go with their memory on a hard delete (FK); a
   forget whose unlocked read is older than a concurrent edit logs the
   committed version.
4. The erasure scrub pseudonymizes the subject's events, NULLs their session
   and strips ``details`` from ``prior``, leaving the erased / scrubbed markers (#2043 §1e).
   On memories the subject only edited, the row after their details edit is
   stripped, judged by that memory's log alone and counting edits an earlier
   erasure already stripped (#2235).
5. ``f03_2043_memory_versions`` round trip: existing rows read ``version = 1``;
   the downgrade drops the table and the column.
6. One clock (#2135): remember / update_memory / forget stamp the row and the
   event from the server clock, so a window over server-clock times finds the
   created events even with the database clock an hour ahead.

The vector store, permissions and capacity gate are stubbed. Every seeded row
is deleted again. Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory, MemoryRevision
from models.schemas import RememberRequest, UpdateMemoryRequest
from services import session_rollback as sr
from services.account_erasure_service import AccountErasureService
from services.erasure_details import (
    StripCounts,
    lock_subject_memories,
    pseudonymize_revision_actor,
    strip_subject_details,
    subject_memory_ids,
)
from services.memory_listing import changes_in_session
from services.memory_service import MemoryService
from services.quota_service import DailyReservation
from services.write_provenance import SessionPrincipal
from utils.datetime import utcnow
from utils.exceptions import VersionConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")

_TIMEOUT = 15


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(async_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """A committed workspace / context / memory at version 3 (visible to every session)."""
    owner = f"versions-it-{uuid.uuid4().hex[:8]}"
    ws_id, ctx_id, mem_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        session.add(Workspace(id=ws_id, name=f"ws-{owner}", owner_user_id=owner))
        await session.flush()
        session.add(Context(id=ctx_id, workspace_id=ws_id, name="versions", created_by=owner))
        await session.flush()
        session.add(
            Memory(
                id=mem_id,
                user_id=owner,
                workspace_id=ws_id,
                context_id=ctx_id,
                summary="the versioned memory",
                content="first body",
                type="note",
                importance=0.5,
                tags=["a"],
                details={"k": "v"},
                client="test",
                embedding_status="success",
                version=3,
            )
        )
        await session.commit()
    try:
        yield {"owner": owner, "workspace_id": ws_id, "context_id": ctx_id, "memory_id": mem_id}
    finally:
        async with _maker(async_engine)() as session:
            # contexts → memories → memory_revisions cascade with the workspace
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()


def _service(session: AsyncSession) -> MemoryService:
    svc = MemoryService(session)
    svc._ensure_capacity = AsyncMock()  # type: ignore[method-assign]
    svc._lint_write = AsyncMock(return_value=[])  # type: ignore[method-assign]
    svc._update_sync_qdrant_payload = AsyncMock()  # type: ignore[method-assign]
    return svc


async def _state(engine: AsyncEngine, memory_id: uuid.UUID) -> tuple[Memory, list[MemoryRevision]]:
    async with _maker(engine)() as session:
        memory = (await session.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()
        revisions = list(
            (
                await session.execute(
                    select(MemoryRevision)
                    .where(MemoryRevision.memory_id == memory_id)
                    .order_by(MemoryRevision.id)
                )
            ).scalars()
        )
        return memory, revisions


async def _two_overlapping_edits(
    engine: AsyncEngine, memory_id: uuid.UUID, first: dict[str, Any], second: dict[str, Any]
) -> list[Any]:
    """Run edit A, hold it after its row lock, start edit B, then let A commit.

    B reaches its own ``FOR UPDATE`` while A still holds the row, so B's
    re-read waits for A's commit — the race the version check must win.
    """
    a_locked, b_locking = asyncio.Event(), asyncio.Event()
    maker = _maker(engine)

    async def run(fields: dict[str, Any], is_a: bool) -> Any:
        async with maker() as session:
            svc = _service(session)
            original_lock = svc._lock_for_edit

            async def lock(memory_id: uuid.UUID, **kw: Any) -> Any:
                if not is_a:
                    b_locking.set()
                return await original_lock(memory_id, **kw)

            async def hold(*_a: Any, **_k: Any) -> None:
                # A's qdrant payload sync runs under its row lock, before commit.
                a_locked.set()
                await asyncio.wait_for(b_locking.wait(), _TIMEOUT)
                await asyncio.sleep(0.3)  # B is now blocked on A's row lock

            svc._lock_for_edit = lock  # type: ignore[method-assign]
            if is_a:
                svc._update_sync_qdrant_payload = hold  # type: ignore[method-assign]
            try:
                return await svc._update_in_place(
                    UpdateMemoryRequest(memory_id=memory_id, **fields), user_id="editor"
                )
            except VersionConflictError as e:
                return e

    with (
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
    ):
        task_a = asyncio.create_task(run(first, True))
        await asyncio.wait_for(a_locked.wait(), _TIMEOUT)
        task_b = asyncio.create_task(run(second, False))
        return await asyncio.wait_for(asyncio.gather(task_a, task_b), _TIMEOUT)


async def test_two_edits_with_the_same_expected_version_one_wins(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    memory_id = seeded["memory_id"]
    a, b = await _two_overlapping_edits(
        async_engine,
        memory_id,
        {"importance": 0.9, "expected_version": 3},
        {"tags": ["b"], "expected_version": 3},
    )
    assert a.version == 4
    assert isinstance(b, VersionConflictError)
    assert (b.expected_version, b.current_version) == (3, 4)

    memory, revisions = await _state(async_engine, memory_id)
    assert memory.version == 4
    assert memory.importance == 0.9 and memory.tags == ["a"]  # B wrote nothing
    assert [(r.kind, r.version, r.prior) for r in revisions] == [
        ("updated", 4, {"importance": 0.5})
    ]
    assert revisions[0].actor_user_id == "editor"
    assert revisions[0].context_id == seeded["context_id"]
    assert revisions[0].at == memory.updated_at


async def test_two_unconditional_edits_both_count(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """Last writer wins on the fields, but neither version step is lost."""
    memory_id = seeded["memory_id"]
    a, b = await _two_overlapping_edits(
        async_engine, memory_id, {"importance": 0.9}, {"tags": ["b"]}
    )
    assert (a.version, b.version) == (4, 5)
    memory, revisions = await _state(async_engine, memory_id)
    assert memory.version == 5 and memory.importance == 0.9 and memory.tags == ["b"]
    assert [(r.version, r.prior) for r in revisions] == [
        (4, {"importance": 0.5}),
        (5, {"tags": ["a"]}),
    ]


async def test_revisions_go_with_a_hard_deleted_memory(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    memory_id = seeded["memory_id"]
    async with _maker(async_engine)() as session:
        session.add(
            MemoryRevision(
                memory_id=memory_id,
                context_id=seeded["context_id"],
                version=3,
                kind="forgotten",
                writer="api",
                actor_user_id="someone",
            )
        )
        await session.commit()
        await session.execute(delete(Memory).where(Memory.id == memory_id))
        await session.commit()
        left = await session.execute(
            text("SELECT count(*) FROM memory_revisions WHERE memory_id = :m"), {"m": memory_id}
        )
        assert left.scalar_one() == 0


async def test_forget_logs_the_version_it_deleted(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """forget reads the memory without a lock; an edit that commits before
    forget's UPDATE moves the version, and the forgotten event carries it."""
    memory_id = seeded["memory_id"]
    maker = _maker(async_engine)
    async with maker() as forgetter, maker() as editor:
        svc = MemoryService(forgetter)
        memory = await svc.memory_repo.get(memory_id)
        assert memory is not None and memory.version == 3
        await editor.execute(update(Memory).where(Memory.id == memory_id).values(version=4))
        await editor.commit()

        svc._delete_memory_point = AsyncMock()  # type: ignore[method-assign]
        await svc._soft_delete_memory(
            "editor", memory, str(seeded["workspace_id"]), str(seeded["context_id"])
        )
        await forgetter.commit()

    memory, revisions = await _state(async_engine, memory_id)
    assert memory.deleted_at is not None and memory.version == 4
    assert [(r.kind, r.version, r.at) for r in revisions] == [("forgotten", 4, memory.deleted_at)]


async def test_erasure_scrubs_the_revision_log(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    from services.account_erasure_service import AccountErasureService

    subject, other = seeded["owner"], "another-member"
    ctx = seeded["context_id"]
    other_memory = uuid.uuid4()
    async with _maker(async_engine)() as session:
        session.add(
            Memory(
                id=other_memory,
                user_id=other,
                workspace_id=seeded["workspace_id"],
                context_id=ctx,
                summary="someone else's memory",
                content="body",
                type="note",
                client="test",
            )
        )
        await session.flush()
        session.add_all(
            [
                # the subject's own edit of their memory, in a session
                MemoryRevision(
                    memory_id=seeded["memory_id"],
                    context_id=ctx,
                    version=2,
                    kind="updated",
                    writer="api",
                    actor_user_id=subject,
                    session_id="sess-1",
                    prior={"summary": "old", "details": {"lat": 1}},
                ),
                # another member's edit of the subject's memory
                MemoryRevision(
                    memory_id=seeded["memory_id"],
                    context_id=ctx,
                    version=3,
                    kind="updated",
                    writer="api",
                    actor_user_id=other,
                    session_id="sess-2",
                    prior={"details": {"lat": 2}, "importance": 0.4},
                ),
                # another member's edit of their own memory: untouched
                MemoryRevision(
                    memory_id=other_memory,
                    context_id=ctx,
                    version=2,
                    kind="updated",
                    writer="api",
                    actor_user_id=other,
                    session_id="sess-3",
                    prior={"details": {"keep": True}},
                ),
            ]
        )
        await session.commit()

        service = AccountErasureService.__new__(AccountErasureService)
        service.db = session
        with patch("services.account_erasure_service._audit_salt", return_value="salt"):
            counts = await service._erase_memory_revisions(subject)
            pseudonym = service._pseudonym(subject)
        await session.commit()
        # the subject's memory: both events' priors lose their details
        assert counts == {"memory_revisions_pseudonymized": 1, "revision_details_stripped": 2}

        rows = (
            await session.execute(
                text(
                    "SELECT actor_user_id, session_id, prior FROM memory_revisions "
                    "WHERE context_id = :c ORDER BY id"
                ),
                {"c": ctx},
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        # the subject's memory: details gone from every event, live scrub marked
        (pseudonym, None, {"summary": "old", "erased": ["details"], "scrubbed": ["details"]}),
        (other, "sess-2", {"importance": 0.4, "erased": ["details"], "scrubbed": ["details"]}),
        (other, "sess-3", {"details": {"keep": True}}),
    ]


def _edit(
    ctx: uuid.UUID,
    memory_id: uuid.UUID,
    version: int,
    actor: str,
    prior: dict[str, Any],
    session_id: str | None = None,
) -> MemoryRevision:
    return MemoryRevision(
        memory_id=memory_id,
        context_id=ctx,
        version=version,
        kind="updated",
        writer="api",
        actor_user_id=actor,
        prior=prior,
        session_id=session_id,
    )


async def _seed_log(
    session: AsyncSession,
    seeded: dict[str, Any],
    owners: dict[uuid.UUID, str],
    edits: list[MemoryRevision],
) -> None:
    for memory_id, owner in owners.items():
        session.add(
            Memory(
                id=memory_id,
                user_id=owner,
                workspace_id=seeded["workspace_id"],
                context_id=seeded["context_id"],
                summary="not the subject's memory",
                content="body",
                type="note",
                client="test",
            )
        )
    await session.flush()
    session.add_all(edits)
    await session.commit()


async def _erase_revisions(session: AsyncSession, subject: str) -> str:
    """Run the revision-log erasure for ``subject``; returns their pseudonym."""
    from services.account_erasure_service import AccountErasureService

    service = AccountErasureService.__new__(AccountErasureService)
    service.db = session
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        await service._erase_memory_revisions(subject)
        pseudonym = service._pseudonym(subject)
    await session.commit()
    return pseudonym


async def _log(session: AsyncSession, *memory_ids: uuid.UUID) -> list[tuple[Any, ...]]:
    """``(memory_id, version, actor, prior)`` per revision, in ``memory_ids`` order."""
    rows = (
        await session.execute(
            select(
                MemoryRevision.memory_id,
                MemoryRevision.version,
                MemoryRevision.actor_user_id,
                MemoryRevision.prior,
            ).where(MemoryRevision.memory_id.in_(memory_ids))
        )
    ).all()
    return sorted((tuple(r) for r in rows), key=lambda r: (memory_ids.index(r[0]), r[1]))


async def test_erasure_strips_the_subjects_details_from_a_memory_they_only_edited(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2235: the "no details edit in between" check looks at this memory only.

    The subject edits the details of another member's memory; the next
    details edit there holds the subject's value in its prior and must be
    stripped. An unrelated memory with two later details edits used to make
    the uncorrelated inner ``NOT EXISTS`` false for every row, so the
    subject's value stayed. Rows that never held the subject's value are
    untouched.
    """
    subject, author, third = seeded["owner"], "edited-author", "third-member"
    ctx = seeded["context_id"]
    edited, unrelated = uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        await _seed_log(
            session,
            seeded,
            {edited: author, unrelated: third},
            [
                # the subject overwrites the author's details (prior = the author's)
                _edit(ctx, edited, 2, subject, {"details": {"by": "author"}}),
                # a non-details edit: no details in its prior
                _edit(ctx, edited, 3, author, {"importance": 0.4}),
                # the next details edit: its prior is the subject's value
                _edit(ctx, edited, 4, author, {"details": {"by": "subject"}}),
                # a later details edit: its prior is the author's own value
                _edit(ctx, edited, 5, third, {"details": {"by": "author-again"}}),
                # an unrelated memory with details edits after version 2
                _edit(ctx, unrelated, 3, third, {"details": {"u": 1}}),
                _edit(ctx, unrelated, 4, third, {"details": {"u": 2}}),
                _edit(ctx, unrelated, 6, third, {"details": {"u": 3}}),
            ],
        )
        pseudonym = await _erase_revisions(session, subject)
        log = await _log(session, edited, unrelated)
    assert log == [
        (edited, 2, pseudonym, {"details": {"by": "author"}}),
        (edited, 3, author, {"importance": 0.4}),
        (edited, 4, author, {"erased": ["details"]}),
        (edited, 5, third, {"details": {"by": "author-again"}}),
        (unrelated, 3, third, {"details": {"u": 1}}),
        (unrelated, 4, third, {"details": {"u": 2}}),
        (unrelated, 6, third, {"details": {"u": 3}}),
    ]


async def test_a_second_erasure_finds_an_edit_the_first_one_stripped(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2235 review: two editors of one memory erased one after the other.

    Erasing the first editor strips the second editor's row (its prior held
    the first editor's value). That row is still the second editor's details
    edit, so erasing them strips the next details edit, whose prior holds
    their value.
    """
    first, second, author, third = seeded["owner"], "second-editor", "the-author", "third"
    ctx = seeded["context_id"]
    edited = uuid.uuid4()
    async with _maker(async_engine)() as session:
        await _seed_log(
            session,
            seeded,
            {edited: author},
            [
                _edit(ctx, edited, 2, first, {"details": {"by": "author"}}),
                _edit(ctx, edited, 4, second, {"details": {"by": "first"}}),
                _edit(ctx, edited, 6, third, {"details": {"by": "second"}}),
            ],
        )
        first_pseudonym = await _erase_revisions(session, first)
        second_pseudonym = await _erase_revisions(session, second)
        log = await _log(session, edited)
    assert log == [
        (edited, 2, first_pseudonym, {"details": {"by": "author"}}),
        (edited, 4, second_pseudonym, {"erased": ["details"]}),
        (edited, 6, third, {"erased": ["details"]}),
    ]


async def test_an_already_stripped_edit_in_between_ends_the_subjects_value(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2235 review: a details edit whose prior was stripped earlier (it held
    the subject's value) still counts as an edit in between, so the later
    edit — whose prior is another member's value — keeps its details."""
    subject, author, other = seeded["owner"], "the-author", "other-member"
    ctx = seeded["context_id"]
    edited = uuid.uuid4()
    async with _maker(async_engine)() as session:
        await _seed_log(
            session,
            seeded,
            {edited: author},
            [
                _edit(ctx, edited, 2, subject, {"details": {"by": "author"}}),
                _edit(ctx, edited, 3, other, {"erased": ["details"]}),
                _edit(ctx, edited, 5, author, {"details": {"by": "other"}}),
            ],
        )
        pseudonym = await _erase_revisions(session, subject)
        log = await _log(session, edited)
    assert log == [
        (edited, 2, pseudonym, {"details": {"by": "author"}}),
        (edited, 3, other, {"erased": ["details"]}),
        (edited, 5, author, {"details": {"by": "other"}}),
    ]


def _erasure_service(session: AsyncSession) -> AccountErasureService:
    service = AccountErasureService.__new__(AccountErasureService)
    service.db = session
    return service


# Whole-table byte-level snapshots (every tenant), keyed by id.
_REVISIONS_OUTSIDE = text(
    "SELECT t.id, to_jsonb(t) FROM memory_revisions AS t WHERE NOT (t.memory_id = ANY(:excluded))"
)
_ALL_MEMORIES = text("SELECT t.id, to_jsonb(t) FROM memories AS t")


async def _snapshot(
    session: AsyncSession, query: Any, excluded: list[uuid.UUID] | None = None
) -> dict[Any, Any]:
    rows = (await session.execute(query, {"excluded": excluded or []})).all()
    return {r[0]: r[1] for r in rows}


async def _sessions(session: AsyncSession, *memory_ids: uuid.UUID) -> list[tuple[Any, ...]]:
    """``(memory_id, version, session_id)`` per revision, in ``memory_ids`` order."""
    rows = (
        await session.execute(
            select(
                MemoryRevision.memory_id, MemoryRevision.version, MemoryRevision.session_id
            ).where(MemoryRevision.memory_id.in_(memory_ids))
        )
    ).all()
    return sorted((tuple(r) for r in rows), key=lambda r: (memory_ids.index(r[0]), r[1]))


async def test_erasure_leaves_other_users_revisions_untouched(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2242: each of the three scoped UPDATEs rewrites the subject's rows only.

    Every revision row outside the two memories the subject authored or
    edited — in this context and every other one in the database — and every
    ``memories`` row are byte-identical afterwards, including another
    member's rows on a memory the subject authored the scope reaches through
    (``third`` edited it), an earlier erased member's already-stripped edit
    and a pseudonym-looking actor.
    """
    subject, author, other = seeded["owner"], "the-author", "other-member"
    third, fourth, ghost = "third-member", "fourth-member", "f" * 64
    ctx = seeded["context_id"]
    authored, by_third, by_other, edited, by_author = (uuid.uuid4() for _ in range(5))
    async with _maker(async_engine)() as session:
        await _seed_log(
            session,
            seeded,
            {
                authored: subject,
                by_third: third,
                by_other: other,
                edited: author,
                by_author: author,
            },
            [
                # the subject's memory, edited by third: stripped and marked
                _edit(ctx, authored, 2, third, {"details": {"s": 1}}, "sess-s"),
                # third's memory: two details edits by third, one by fourth
                _edit(ctx, by_third, 2, third, {"details": {"u": 1}}, "sess-u1"),
                _edit(ctx, by_third, 3, fourth, {"details": {"u": 2}}, "sess-u2"),
                _edit(ctx, by_third, 4, third, {"details": {"u": 3}}),
                # other's memory: an earlier erased member's stripped edit
                _edit(ctx, by_other, 2, ghost, {"erased": ["details"]}, "sess-g"),
                # the author's memory the subject edited
                _edit(ctx, edited, 2, subject, {"details": {"by": "author"}}, "sess-e"),
                _edit(ctx, edited, 4, author, {"details": {"by": "subject"}}, "sess-a"),
                # the author's memory only the author edited
                _edit(ctx, by_author, 2, author, {"details": {"w": 1}}, "sess-w"),
                _edit(ctx, by_author, 3, author, {"details": {"w": 2}}),
            ],
        )
        # the subject's memories: the seeded one (no revisions), the one
        # they authored and the one they edited
        in_scope = [seeded["memory_id"], authored, edited]
        revisions_before = await _snapshot(session, _REVISIONS_OUTSIDE, in_scope)
        memories_before = await _snapshot(session, _ALL_MEMORIES)
        await session.commit()

        pseudonym = _erasure_service(session)._pseudonym(subject)
        stripped = await strip_subject_details(session, subject)
        pseudonymized = await pseudonymize_revision_actor(session, subject, pseudonym)
        await session.commit()

        assert stripped == StripCounts(details_stripped=2, scrub_marked=1)
        assert pseudonymized == 1
        # a re-run matches nothing: stripped priors lack ``details`` and the
        # authored memory's event already carries the scrubbed marker
        assert await strip_subject_details(session, subject) == StripCounts(0, 0)
        await session.commit()
        assert await _snapshot(session, _REVISIONS_OUTSIDE, in_scope) == revisions_before
        # this step never writes ``memories`` (the memory scrub runs after it)
        assert await _snapshot(session, _ALL_MEMORIES) == memories_before
        assert await _log(session, authored, edited) == [
            (authored, 2, third, {"erased": ["details"], "scrubbed": ["details"]}),
            (edited, 2, pseudonym, {"details": {"by": "author"}}),
            (edited, 4, author, {"erased": ["details"]}),
        ]
        assert await _sessions(session, authored, edited) == [
            (authored, 2, "sess-s"),
            (edited, 2, None),
            (edited, 4, "sess-a"),
        ]


async def _pid(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")) or 0)


async def _wait_until_blocked(probe: AsyncSession, waiter_pid: int, holder_pid: int) -> None:
    """Poll until ``waiter_pid`` waits on a lock ``holder_pid`` holds."""
    deadline = asyncio.get_running_loop().time() + _TIMEOUT
    while True:
        blockers = await probe.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})
        await probe.rollback()
        if holder_pid in (blockers or []):
            return
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"pid {waiter_pid} never blocked on {holder_pid}: {blockers}")
        await asyncio.sleep(0.05)


async def test_revision_updates_lock_in_id_order(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2242: the strip locks its rows in ascending id and waits on a held one.

    Two rows of a memory the subject authored are stripped; the lower id has
    the HIGHER version and sits physically after the other (a no-op update
    moved it), so an index scan by ``(memory_id, version)`` and a heap scan
    both reach the higher id first. Another session holds the higher id. The
    erasure must block on it (no ``SKIP LOCKED``) while already holding the
    lower id — which it can only have locked first in id order.
    """
    subject, other = seeded["owner"], "other-member"
    ctx = seeded["context_id"]
    authored = uuid.uuid4()
    async with _maker(async_engine)() as session:
        await _seed_log(session, seeded, {authored: subject}, [])
        low_row = _edit(ctx, authored, 3, other, {"details": {"n": 3}})
        session.add(low_row)
        await session.flush()
        high_row = _edit(ctx, authored, 2, other, {"details": {"n": 2}})
        session.add(high_row)
        await session.flush()
        low, high = low_row.id, high_row.id
        assert low < high
        await session.execute(
            text("UPDATE memory_revisions SET writer = writer WHERE id = :id"), {"id": low}
        )
        await session.commit()

    factory = _maker(async_engine)
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        async with factory() as holder, factory() as writer, factory() as probe:
            # read inside the transactions that hold / wait (NullPool)
            holder_pid, writer_pid = await _pid(holder), await _pid(writer)
            await holder.execute(
                text("SELECT id FROM memory_revisions WHERE id = :id FOR UPDATE"), {"id": high}
            )
            erase = asyncio.create_task(_erasure_service(writer)._erase_memory_revisions(subject))
            held: DBAPIError | None = None
            try:
                await _wait_until_blocked(probe, writer_pid, holder_pid)
                try:
                    await probe.execute(
                        text("SELECT id FROM memory_revisions WHERE id = :id FOR UPDATE NOWAIT"),
                        {"id": low},
                    )
                except DBAPIError as exc:
                    held = exc
                await probe.rollback()
                blocked = not erase.done()
            finally:
                # release everything before judging, so a failure never leaves
                # the writer waiting (and the fixture teardown behind it)
                await holder.commit()
                pseudonymized = await asyncio.wait_for(erase, _TIMEOUT)
            await writer.commit()
            assert blocked
            # the writer held the lower id while it waited on the higher one
            assert held is not None
            assert getattr(held.orig, "sqlstate", None) == "55P03"  # lock_not_available
            # nothing the subject did themselves: no row to pseudonymize
            assert pseudonymized["memory_revisions_pseudonymized"] == 0
            assert await _log(probe, authored) == [
                (authored, 2, other, {"erased": ["details"], "scrubbed": ["details"]}),
                (authored, 3, other, {"erased": ["details"], "scrubbed": ["details"]}),
            ]


_MEMORIES_BUT = text(
    "SELECT t.id, to_jsonb(t) FROM memories AS t WHERE NOT (t.id = ANY(:excluded))"
)
_CONTEXTS_BUT = text(
    "SELECT t.id, to_jsonb(t) FROM contexts AS t WHERE NOT (t.id = ANY(:excluded))"
)
# a cleared row, apart from the cleared column (and the ORM's onupdate stamp)
_CLEARED_MEMORIES = text(
    "SELECT t.id, to_jsonb(t) - 'deleted_by' - 'updated_at' FROM memories AS t "
    "WHERE t.id = ANY(:ids)"
)
_CLEARED_CONTEXTS = text(
    "SELECT t.id, to_jsonb(t) - 'deleted_by' - 'updated_at' FROM contexts AS t "
    "WHERE t.id = ANY(:ids)"
)


async def _rows(session: AsyncSession, query: Any, ids: list[uuid.UUID]) -> dict[Any, Any]:
    return {r[0]: r[1] for r in (await session.execute(query, {"excluded": ids, "ids": ids})).all()}


async def test_deleted_by_clear_locks_in_id_order(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2263: the ``deleted_by`` clear locks its rows in ascending id and waits.

    Two tombstones of another member's memories carry the subject as
    ``deleted_by`` — rows outside the subject's memory locks. The higher id
    sits physically first, so a heap scan reaches it first. Another session
    holds the higher id; the scrub must block on it while already holding the
    lower one, which it can only have locked first in id order. Afterwards
    both lose ``deleted_by`` and nothing else; a tombstone another member
    deleted, every other memory and context, are byte-identical — and so is a
    context the subject deleted, apart from ``deleted_by``.
    """
    subject, other = seeded["owner"], "other-member"
    ws, ctx = seeded["workspace_id"], seeded["context_id"]
    low, high = sorted((uuid.uuid4(), uuid.uuid4()))
    unrelated = uuid.uuid4()
    deleted_ctx, other_ctx = uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        for memory_id, deleted_by in ((high, subject), (low, subject), (unrelated, other)):
            session.add(
                Memory(
                    id=memory_id,
                    user_id=other,
                    workspace_id=ws,
                    context_id=ctx,
                    summary="another member's tombstone",
                    content="body",
                    type="note",
                    client="test",
                    details={"k": str(memory_id)},
                    deleted_at=utcnow(),
                    deleted_by=deleted_by,
                )
            )
            await session.flush()  # one at a time: ``high`` first in the heap
        for context_id, deleted_by in ((deleted_ctx, subject), (other_ctx, other)):
            session.add(
                Context(
                    id=context_id,
                    workspace_id=ws,
                    name=f"deleted-{context_id.hex[:6]}",
                    created_by=other,
                    deleted_at=utcnow(),
                    deleted_by=deleted_by,
                )
            )
        await session.commit()
        cleared = [low, high]
        memories_before = await _rows(session, _MEMORIES_BUT, [*cleared, seeded["memory_id"]])
        contexts_before = await _rows(session, _CONTEXTS_BUT, [deleted_ctx])
        cleared_before = await _rows(session, _CLEARED_MEMORIES, cleared)
        ctx_before = await _rows(session, _CLEARED_CONTEXTS, [deleted_ctx])
        await session.commit()

    factory = _maker(async_engine)
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        async with factory() as holder, factory() as writer, factory() as probe:
            holder_pid, writer_pid = await _pid(holder), await _pid(writer)
            await holder.execute(
                text("SELECT id FROM memories WHERE id = :id FOR UPDATE"), {"id": high}
            )
            scrub = asyncio.create_task(_erasure_service(writer)._scrub_surviving_memories(subject))
            held: DBAPIError | None = None
            try:
                await _wait_until_blocked(probe, writer_pid, holder_pid)
                try:
                    await probe.execute(
                        text("SELECT id FROM memories WHERE id = :id FOR UPDATE NOWAIT"),
                        {"id": low},
                    )
                except DBAPIError as exc:
                    held = exc
                await probe.rollback()
                blocked = not scrub.done()
            finally:
                await holder.commit()
                scrubbed = await asyncio.wait_for(scrub, _TIMEOUT)
            await writer.commit()
            assert blocked
            assert held is not None
            assert getattr(held.orig, "sqlstate", None) == "55P03"  # lock_not_available
            assert scrubbed == 1  # the subject's own memory (the fixture's)

    async with _maker(async_engine)() as session:
        deleted_by = dict(
            (
                await session.execute(
                    select(Memory.id, Memory.deleted_by).where(
                        Memory.id.in_([low, high, unrelated])
                    )
                )
            ).all()
        )
        assert deleted_by == {low: None, high: None, unrelated: other}
        assert await _rows(session, _CLEARED_MEMORIES, cleared) == cleared_before
        assert all(row["deleted_at"] is not None for row in cleared_before.values())
        assert (
            await session.scalar(select(Context.deleted_by).where(Context.id == deleted_ctx))
        ) is None
        assert await _rows(session, _CLEARED_CONTEXTS, [deleted_ctx]) == ctx_before
        assert (
            await _rows(session, _MEMORIES_BUT, [*cleared, seeded["memory_id"]]) == memories_before
        )
        assert await _rows(session, _CONTEXTS_BUT, [deleted_ctx]) == contexts_before


async def test_the_erasure_lock_holds_the_memories_the_subject_deleted(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2263: ``lock_subject_memories(deleted_by=True)`` holds, in its one
    ordered statement, another member's tombstone the subject deleted, next
    to the subject's own memory; a tombstone someone else deleted is not
    locked, and without the flag the deleted one is not either."""
    subject, other = seeded["owner"], "other-member"
    deleted, unrelated = uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        for memory_id, deleted_by in ((deleted, subject), (unrelated, other)):
            session.add(
                Memory(
                    id=memory_id,
                    user_id=other,
                    workspace_id=seeded["workspace_id"],
                    context_id=seeded["context_id"],
                    summary="another member's tombstone",
                    content="body",
                    type="note",
                    client="test",
                    deleted_at=utcnow(),
                    deleted_by=deleted_by,
                )
            )
        await session.commit()

    async def locked(flag: bool) -> set[uuid.UUID]:
        factory = _maker(async_engine)
        async with factory() as eraser, factory() as probe:
            await lock_subject_memories(eraser, subject, deleted_by=flag)
            held: set[uuid.UUID] = set()
            try:
                for memory_id in (seeded["memory_id"], deleted, unrelated):
                    try:
                        await probe.execute(
                            text("SELECT id FROM memories WHERE id = :id FOR UPDATE NOWAIT"),
                            {"id": memory_id},
                        )
                    except DBAPIError as exc:
                        assert getattr(exc.orig, "sqlstate", None) == "55P03"
                        held.add(memory_id)
                    await probe.rollback()
            finally:
                await eraser.rollback()
            return held

    assert await locked(True) == {seeded["memory_id"], deleted}
    assert await locked(False) == {seeded["memory_id"]}


async def test_an_edit_waits_for_the_erasure_and_copies_the_scrubbed_details(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2242 review: the subject's memories are locked before the strip.

    An edit of a memory the subject authored, arriving after the strip took
    its snapshot, would otherwise copy the subject's live ``details`` into a
    new revision's prior that nothing strips (the memory scrub NULLs only the
    live value). It waits on the memory lock instead and, once the erasure
    commits, copies the scrubbed value.
    """
    subject, third = seeded["owner"], "third-member"
    ctx = seeded["context_id"]
    authored = uuid.uuid4()
    async with _maker(async_engine)() as session:
        await _seed_log(
            session,
            seeded,
            {authored: subject},
            [_edit(ctx, authored, 2, third, {"importance": 1})],
        )
        await session.execute(
            update(Memory)
            .where(Memory.id == authored)
            .values(details={"secret": "the subject's"}, version=2)
        )
        await session.commit()

    factory = _maker(async_engine)

    async def edit() -> Any:
        async with factory() as session:
            return await _service(session)._update_in_place(
                UpdateMemoryRequest(memory_id=authored, details={"by": "editor"}),
                user_id="editor",
            )

    with (
        patch("services.account_erasure_service._audit_salt", return_value="salt"),
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
    ):
        async with factory() as eraser, factory() as probe:
            eraser_pid = await _pid(eraser)
            await lock_subject_memories(eraser, subject)
            editor = asyncio.create_task(edit())
            try:
                deadline = asyncio.get_running_loop().time() + _TIMEOUT
                while not await probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE :pid = ANY(pg_blocking_pids(pid))"
                    ),
                    {"pid": eraser_pid},
                ):
                    await probe.rollback()
                    assert not editor.done(), "the edit did not wait for the erasure"
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.05)
                await probe.rollback()
                service = _erasure_service(eraser)
                await service._erase_memory_revisions(subject)
                await service._scrub_surviving_memories(subject)
            finally:
                await eraser.commit()
            await asyncio.wait_for(editor, _TIMEOUT)

        async with factory() as session:
            log = await _log(session, authored)
    assert log[-1][2:] == ("editor", {"details": None})
    assert all("the subject's" not in str(prior) for *_, prior in log)


async def test_strip_by_pseudonym_is_idempotent_after_erasure(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2242, the #2243 hook: statements 1 and 2 re-run by pseudonym.

    After a full erasure they match nothing. A value an erasure before #2235
    left behind (the details edit right after the subject's) is found by the
    pseudonym and is the only row stripped.
    """
    subject, author, third = seeded["owner"], "the-author", "third-member"
    ctx = seeded["context_id"]
    edited, authored = uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        await _seed_log(
            session,
            seeded,
            {edited: author, authored: subject},
            [
                _edit(ctx, edited, 2, subject, {"details": {"by": "author"}}),
                _edit(ctx, edited, 4, author, {"details": {"by": "subject"}}),
                _edit(ctx, authored, 2, third, {"details": {"s": 1}}),
            ],
        )
        service = _erasure_service(session)
        with patch("services.account_erasure_service._audit_salt", return_value="salt"):
            await service._erase_memory_revisions(subject)
            await service._scrub_surviving_memories(subject)
            pseudonym = service._pseudonym(subject)
        await session.commit()
        erased_log = await _log(session, edited, authored)
        assert erased_log == [
            (edited, 2, pseudonym, {"details": {"by": "author"}}),
            (edited, 4, author, {"erased": ["details"]}),
            (authored, 2, third, {"erased": ["details"], "scrubbed": ["details"]}),
        ]

    async with _maker(async_engine)() as session:
        # the authored memory's event is marked already: counted 0, not again
        assert await strip_subject_details(session, pseudonym) == StripCounts(0, 0)
        await session.commit()
        assert await _log(session, edited, authored) == erased_log

        # left behind: the subject's (pseudonymized) details edit and the next
        # details edit, whose prior is the subject's value, never stripped
        session.add_all(
            [
                _edit(ctx, edited, 6, pseudonym, {"details": {"by": "author-again"}}),
                _edit(ctx, edited, 8, third, {"details": {"by": "subject-again"}}),
            ]
        )
        await session.commit()
        assert await strip_subject_details(session, pseudonym) == StripCounts(1, 0)
        await session.commit()
        assert await _log(session, edited, authored) == [
            *erased_log[:2],
            (edited, 6, pseudonym, {"details": {"by": "author-again"}}),
            (edited, 8, third, {"erased": ["details"]}),
            erased_log[2],
        ]


async def test_subject_memory_ids_by_raw_id_and_by_pseudonym_agree(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    """#2242: the scope finds the same memories by the raw sub before
    erasure and by the pseudonym after it — authored ones without a revision
    row (pre-f03) included, other members' memories excluded."""
    subject, author = seeded["owner"], "the-author"
    ctx = seeded["context_id"]
    authored, edited, unrelated = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        await _seed_log(
            session,
            seeded,
            {authored: subject, edited: author, unrelated: author},
            [
                _edit(ctx, edited, 2, subject, {"details": {"by": "author"}}),
                _edit(ctx, unrelated, 2, author, {"details": {"u": 1}}),
            ],
        )
        expected = {seeded["memory_id"], authored, edited}
        assert set((await session.scalars(subject_memory_ids(subject))).all()) == expected

        service = _erasure_service(session)
        with patch("services.account_erasure_service._audit_salt", return_value="salt"):
            await service._erase_memory_revisions(subject)
            await service._scrub_surviving_memories(subject)
            pseudonym = service._pseudonym(subject)
        await session.commit()

        assert set((await session.scalars(subject_memory_ids(pseudonym))).all()) == expected
        assert (await session.scalars(subject_memory_ids(subject))).all() == []


def test_f03_round_trip_reads_existing_rows_at_version_one() -> None:
    from tests.integration.test_alembic_migrations import (
        _downgrade_to,
        _reset_alembic_state,
        _sync_engine,
        _upgrade_to,
    )

    f03, prior_head = "f03_2043_memory_versions", "f02_2103_chunks_enabled_at"
    user = f"f03-migration-{uuid.uuid4().hex[:8]}"
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(prior_head)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO memories (id, user_id, workspace_id, summary, content, type, "
                    "embedding_status, importance, confidence, scope, long_term, access_count, "
                    "client, source, created_at) VALUES (gen_random_uuid(), :u, "
                    "gen_random_uuid(), 'seed', 'body', 'note', 'success', 0.5, 1.0, 'working', "
                    "false, 0, 'test', 'mcp_remember', now())"
                ),
                {"u": user},
            )

        _upgrade_to(f03)
        with engine.begin() as conn:
            version = conn.execute(
                text("SELECT version FROM memories WHERE user_id = :u"), {"u": user}
            ).scalar_one()
            assert version == 1
            memory_id = conn.execute(
                text("SELECT id FROM memories WHERE user_id = :u"), {"u": user}
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO memory_revisions (memory_id, context_id, version, kind, "
                    "writer, actor_user_id) VALUES (:m, gen_random_uuid(), 1, 'created', "
                    "'api', :u)"
                ),
                {"m": memory_id, "u": user},
            )
            stored = conn.execute(
                text(
                    "SELECT prior::text, at IS NOT NULL FROM memory_revisions WHERE memory_id = :m"
                ),
                {"m": memory_id},
            ).one()
            assert tuple(stored) == ("{}", True)
            with pytest.raises(Exception, match="valid_revision_kind"):
                with conn.begin_nested():
                    conn.execute(
                        text(
                            "INSERT INTO memory_revisions (memory_id, context_id, version, "
                            "kind, writer, actor_user_id) VALUES (:m, gen_random_uuid(), 1, "
                            "'edited', 'api', :u)"
                        ),
                        {"m": memory_id, "u": user},
                    )

        _downgrade_to(prior_head)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('memory_revisions')")).scalar() is None
            columns = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'memories' AND column_name = 'version'"
                )
            ).all()
            assert columns == []
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM memories WHERE user_id = :u"), {"u": user})
            finally:
                engine.dispose()


# ---------------------------------------------------------------------------
# #2135: one clock for every revision's ``at``
# ---------------------------------------------------------------------------


class _SkewedClock:
    """A server clock one hour behind the database's, ticking 1 s per call.

    Every value it hands out is kept, so a test can prove a stamp came from
    it rather than from the database's ``now()``.
    """

    def __init__(self) -> None:
        self.issued: list[datetime] = []
        self._next = utcnow() - timedelta(hours=1)

    def __call__(self) -> datetime:
        value = self._next
        self._next += timedelta(seconds=1)
        self.issued.append(value)
        return value


async def test_every_revision_at_is_the_server_clock(async_engine: AsyncEngine) -> None:
    """remember, update_memory and forget stamp the row and the event from the
    server clock alone. With that clock an hour behind the database, a window
    over the server-clock times still finds the created events — before
    #2135 a created event took the database's ``now()``, fell an hour past
    the window and the window planned around it."""
    tag = uuid.uuid4().hex[:8]
    owner = f"clock-it-{tag}"
    ws_id, ctx_id = uuid.uuid4(), uuid.uuid4()
    maker = _maker(async_engine)
    async with maker() as session:
        session.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
        await session.flush()
        session.add(Workspace(id=ws_id, name=f"ws-{owner}", owner_user_id=owner))
        await session.flush()
        session.add(WorkspaceMember(workspace_id=ws_id, user_id=owner, role=WorkspaceRole.OWNER))
        session.add(Context(id=ctx_id, workspace_id=ws_id, name="clock", created_by=owner))
        await session.commit()
    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(
            return_value=DailyReservation(key="quota:test:memories:day", count=1)
        ),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    clock = _SkewedClock()
    try:
        with (
            patch("services.memory_service.utcnow", clock),
            patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
            patch("services.quota_service.QuotaService", return_value=quota),
            patch(
                "services.permission_service.PermissionService.can_access_memory",
                AsyncMock(return_value=True),
            ),
            patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
        ):
            since = clock._next
            async with maker() as session:
                svc = _service(session)

                async def remember(summary: str) -> uuid.UUID:
                    result = await svc.remember(
                        RememberRequest(summary=summary, content=summary, type="fact"),
                        user_id=owner,
                        client="pytest",
                        current_context_id=ctx_id,
                        current_workspace_id=ws_id,
                    )
                    return result.memory_id

                edited = await remember("a fact the clock test edits and forgets")
                kept = await remember("a fact the clock test only creates")
                await svc._update_in_place(
                    UpdateMemoryRequest(memory_id=edited, importance=0.9), user_id=owner
                )
                await session.commit()
                memory = await svc.memory_repo.get(edited)
                assert memory is not None
                svc._delete_memory_point = AsyncMock()  # type: ignore[method-assign]
                await svc._soft_delete_memory(owner, memory, str(ws_id), str(ctx_id))
                await session.commit()
            until = clock._next

        row, revisions = await _state(async_engine, edited)
        assert [r.kind for r in revisions] == ["created", "updated", "forgotten"]
        created, updated, forgotten = revisions
        assert created.at == row.created_at
        assert updated.at == row.updated_at
        assert forgotten.at == row.deleted_at
        stamps = [r.at for r in revisions]
        assert all(stamp in clock.issued for stamp in stamps)  # the server clock's values
        assert stamps == sorted(stamps) and len(set(stamps)) == 3
        kept_row, (kept_created,) = await _state(async_engine, kept)
        assert kept_created.at == kept_row.created_at and kept_created.at in clock.issued
        assert created.at < kept_created.at  # stamps rise in write order

        async with maker() as session:
            db_now = await session.scalar(text("SELECT now()::timestamp"))
            assert db_now is not None
            # No stamp is the database's: each is an hour behind its clock.
            for stamp in (*stamps, kept_created.at):
                assert db_now - stamp > timedelta(minutes=50)

            principal = SessionPrincipal(owner, None)
            page = await changes_in_session(
                session,
                context_id=ctx_id,
                session_id=None,
                principal=principal,
                owner_user_id=None,
                since=since,
                until=until,
            )
            assert [(c.memory_id, c.kind) for c in page.changes] == [
                (edited, "created"),
                (kept, "created"),
                (edited, "updated"),
                (edited, "forgotten"),
            ]

            plan = await sr.rollback_session(
                session,
                scope=sr.SessionScope(ctx_id, None, principal, since=since, until=until),
                workspace_id=ws_id,
                requested_by=owner,
                dry_run=True,
                classify=lambda _e: "internal_error",
                new_correlation_id=lambda: "corr-clock",
            )
            by_memory = {item.memory_id: item for item in plan.items}
            assert set(by_memory) == {edited, kept}
            assert by_memory[kept].action == sr.ACTION_FORGET  # created in the window
            # every event of the memory is in the window: nothing "after" it
            assert by_memory[edited].cause != sr.CAUSE_MODIFIED_SINCE
            assert plan.events == 4
    finally:
        async with maker() as session:
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.execute(delete(User).where(User.user_id == owner))
            await session.commit()
