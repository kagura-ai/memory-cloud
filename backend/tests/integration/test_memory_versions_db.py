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
   and strips ``details`` from ``prior`` (#2043 §1e).
5. ``f03_2043_memory_versions`` round trip: existing rows read ``version = 1``;
   the downgrade drops the table and the column.

The vector store, permissions and capacity gate are stubbed. Every seeded row
is deleted again. Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.auth import Context, Workspace
from models.memory import Memory, MemoryRevision
from models.schemas import UpdateMemoryRequest
from services.memory_service import MemoryService
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
            count = await service._erase_memory_revisions(subject)
            pseudonym = service._pseudonym(subject)
        await session.commit()
        assert count == 1

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
        (pseudonym, None, {"summary": "old"}),
        (other, "sess-2", {"importance": 0.4}),
        (other, "sess-3", {"details": {"keep": True}}),
    ]


def test_f03_round_trip_reads_existing_rows_at_version_one() -> None:
    from tests.integration.test_alembic_migrations import (
        _downgrade_to,
        _reset_alembic_state,
        _sync_engine,
        _upgrade_to,
    )

    f03, prior_head = "f03_2043_memory_versions", "f01_2015_content_chunks"
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
