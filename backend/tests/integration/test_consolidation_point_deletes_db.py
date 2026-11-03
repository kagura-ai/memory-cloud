"""Consolidation point deletes after the commit, against a real Postgres (#2565).

The Sleep archive, the Sleep merge's loser and the legacy consolidation task
follow ``forget`` (#2476, #2480): each point is queued in
``deferred_point_deletes`` in the transaction that removes its row, and the
vector store is touched only after the caller's commit, under the point-writer
lock, for points no live row names. A transaction that never commits leaves
its rows live with their points and no queue row; a Sleep phase that fails
takes its queue rows down with its savepoint.

The vector store is stubbed (``delete_points_from_qdrant`` through the session
rollback module's ``_stubs``, ``delete_chunk_points`` here, and the client
under every other vector call, which is how the old per-row delete before the
commit shows up); the lock, the live-owner look and the queue are real. Not
executed in the local unit run — needs the DB container.
"""

# ruff: noqa: F811 — the fixtures are shared with test_session_rollback_db
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from db.qdrant import KAGURA_MEMORIES_COLLECTION
from models.auth import Context
from models.deferred_point_delete import DeferredPointDelete
from models.memory import DELETED_BY_SLEEP_ARCHIVE, DELETED_BY_SLEEP_MERGE, Memory
from models.sleep import SleepReport
from services.sleep.consolidation import ConsolidationPhase
from services.sleep.dedup_merge import DedupMergePhase
from services.sleep.orchestrator import SleepOrchestrator
from services.sleep.reporter import PhaseResult, SleepBudget
from tests.integration.test_session_rollback_db import (  # noqa: F401 — fixtures
    World,
    _stubs,
    world,
)
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_COLLECTION = "kagura_memories_consolidation_it"


@pytest.fixture
def chunks() -> Iterator[AsyncMock]:
    with patch("db.qdrant.delete_chunk_points", AsyncMock()) as mock:
        yield mock


@pytest.fixture(autouse=True)
def client() -> Iterator[MagicMock]:
    """The vector-store client: ``delete_memory_from_qdrant`` (the per-row
    delete these paths ran before their commit) deletes through it."""
    fake = MagicMock()
    fake.delete = AsyncMock()
    with (
        patch("db.qdrant.get_qdrant_client", return_value=fake),
        patch("db.qdrant._active_store", return_value=None),
    ):
        yield fake


async def _queued(world: World, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    return set(
        (
            await world.session.execute(
                select(DeferredPointDelete.memory_id).where(DeferredPointDelete.memory_id.in_(ids))
            )
        ).scalars()
    )


async def _live(world: World, ids: list[uuid.UUID]) -> int:
    return (
        await world.session.execute(
            select(func.count()).where(Memory.id.in_(ids), Memory.deleted_at.is_(None))
        )
    ).scalar_one()


async def _exist(world: World, ids: list[uuid.UUID]) -> int:
    return (
        await world.session.execute(select(func.count()).where(Memory.id.in_(ids)))
    ).scalar_one()


def _deleted_points(points: AsyncMock) -> set[str]:
    return {p for call in points.await_args_list for p in call.args[0]}


async def _old_working(
    world: World, count: int, *, importance: float = 0.1, summary: str = "old working memory"
) -> list[uuid.UUID]:
    """Working rows nobody adopted or read, 40 days old: both archive rules take them."""
    ids = [uuid.uuid4() for _ in range(count)]
    world.session.add_all(
        [
            Memory(
                id=mid,
                user_id=world.owner,
                workspace_id=world.workspace_id,
                context_id=world.context_id,
                summary=f"{summary} {mid.hex[:6]}",
                content="body",
                type="note",
                scope="working",
                importance=importance,
                access_count=0,
                reference_count=0,
                client="test",
                embedding_status="success",
                created_at=utcnow() - timedelta(days=40),
            )
            for mid in ids
        ]
    )
    await world.session.commit()
    return ids


def _commit_fails(world: World) -> Any:
    return patch.object(
        world.session, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))
    )


# ---------------------------------------------------- Sleep, through the caller


async def _sleep_report(world: World) -> uuid.UUID:
    """A full-mode context and the running report the admin trigger pre-creates."""
    context = await world.session.get(Context, world.context_id)
    assert context is not None
    context.sleep_mode = "full"
    report = SleepReport(
        user_id=world.owner, workspace_id=world.workspace_id, context_id=world.context_id
    )
    world.session.add(report)
    await world.session.commit()
    return report.id


def _sleep_patches(world: World, stack: ExitStack, phase: str) -> None:
    """Run only ``phase`` on the caller's session, archive rule switched on."""

    async def get_db() -> AsyncIterator[Any]:
        yield world.session

    graph = MagicMock()
    graph.stats = AsyncMock(return_value={"total_edges": 0})
    graph.get_node_metrics = AsyncMock(return_value=None)
    stack.enter_context(patch("db.base.get_db", get_db))
    stack.enter_context(patch("services.sleep.orchestrator.FULL_PHASES", {phase}))
    stack.enter_context(patch("services.sleep.orchestrator.sleep_suspended", return_value=False))
    stack.enter_context(patch("services.sleep.consolidation.GraphService", return_value=graph))
    stack.enter_context(
        patch(
            "services.sleep.consolidation._adoption_delete_cutoff",
            return_value=utcnow() - timedelta(days=365),
        )
    )


async def _run_sleep(
    world: World, phase: str = "consolidation", *, commit_fails: bool = False
) -> None:
    """The admin trigger's job: the orchestrator, then the caller's commit."""
    from api.routes.admin_sleep import _run_sleep_job

    report_id = await _sleep_report(world)
    with ExitStack() as stack:
        _sleep_patches(world, stack, phase)
        if commit_fails:
            stack.enter_context(_commit_fails(world))
        # Logs a failure and marks the report, never raises.
        await _run_sleep_job(report_id, world.owner, str(world.workspace_id), str(world.context_id))


async def test_a_sleep_archive_whose_commit_fails_keeps_the_rows_and_their_points(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock
) -> None:
    ids = await _old_working(world, 2)
    await _run_sleep(world, commit_fails=True)
    await world.session.rollback()

    assert await _live(world, ids) == 2
    client.delete.assert_not_awaited()
    _stubs["points"].assert_not_awaited()
    chunks.assert_not_awaited()
    assert await _queued(world, ids) == set()


async def test_a_sleep_archive_deletes_the_points_after_the_callers_commit(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock
) -> None:
    ids = await _old_working(world, 2)
    await _run_sleep(world)

    rows = (
        await world.session.execute(
            select(Memory.deleted_at, Memory.deleted_by).where(Memory.id.in_(ids))
        )
    ).all()
    assert {(row.deleted_at is not None, row.deleted_by) for row in rows} == {
        (True, DELETED_BY_SLEEP_ARCHIVE)
    }
    # One point delete and one chunk delete for the collection, nothing per row.
    client.delete.assert_not_awaited()
    assert _deleted_points(_stubs["points"]) == {str(i) for i in ids}
    assert _stubs["points"].await_args.args[1] == KAGURA_MEMORIES_COLLECTION
    assert set(chunks.await_args.args[0]) == set(ids)
    # The fast path dequeued what it handled, under the lock.
    assert await _queued(world, ids) == set()


async def test_a_failed_sleep_phase_takes_its_queue_rows_down_with_its_savepoint(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock
) -> None:
    [memory_id] = await _old_working(world, 1)
    orchestrator = SleepOrchestrator(world.session)
    phase = ConsolidationPhase(world.session, MagicMock())

    async def archive_then_fail(*_: Any, **__: Any) -> PhaseResult:
        memory = await world.session.get(Memory, memory_id)
        assert memory is not None
        assert await phase._archive_tombstone(world.owner, memory)
        assert await _queued(world, [memory_id]) == {memory_id}
        raise RuntimeError("phase failed")

    with patch.object(phase, "execute", archive_then_fail):
        result = await orchestrator._run_phase(
            "consolidation",
            phase,
            MagicMock(),
            world.owner,
            str(world.workspace_id),
            str(world.context_id),
            SleepBudget(),
            report_id=uuid.uuid4(),
        )
    await world.session.commit()
    await orchestrator.delete_committed_points()

    assert result.success is False
    assert orchestrator.pending_point_deletes == []
    assert await _live(world, [memory_id]) == 1
    assert await _queued(world, [memory_id]) == set()
    client.delete.assert_not_awaited()
    _stubs["points"].assert_not_awaited()


# --------------------------------------------------------- Sleep merge's loser


async def _merge_pair(world: World) -> tuple[uuid.UUID, uuid.UUID]:
    winner, loser = await _old_working(world, 2, importance=0.9, summary="near duplicate")
    return winner, loser


def _merge_execute(winner_id: uuid.UUID, loser_id: uuid.UUID) -> Any:
    """``DedupMergePhase.execute`` reduced to one merge of the given pair."""

    async def execute(
        self: DedupMergePhase,
        config: Any,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        budget: SleepBudget,
        **_: Any,
    ) -> PhaseResult:
        winner = await self.db.get(Memory, winner_id)
        loser = await self.db.get(Memory, loser_id)
        assert await self._execute_merge(winner, loser, user_id, workspace_id, context_id)
        return PhaseResult(phase_name="dedup_merge")

    return execute


async def test_a_merge_whose_commit_fails_keeps_the_loser_and_its_point(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock
) -> None:
    winner, loser = await _merge_pair(world)
    with patch.object(DedupMergePhase, "execute", _merge_execute(winner, loser)):
        await _run_sleep(world, "dedup_merge", commit_fails=True)
    await world.session.rollback()

    assert await _live(world, [winner, loser]) == 2
    client.delete.assert_not_awaited()
    _stubs["points"].assert_not_awaited()
    chunks.assert_not_awaited()
    assert await _queued(world, [loser]) == set()


async def test_a_merge_deletes_the_losers_point_after_the_callers_commit(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock
) -> None:
    winner, loser = await _merge_pair(world)
    with patch.object(DedupMergePhase, "execute", _merge_execute(winner, loser)):
        await _run_sleep(world, "dedup_merge")

    gone = await world.session.get(Memory, loser, populate_existing=True)
    assert gone is not None and gone.deleted_by == DELETED_BY_SLEEP_MERGE
    assert await _live(world, [winner]) == 1
    client.delete.assert_not_awaited()
    assert _deleted_points(_stubs["points"]) == {str(loser)}
    assert _stubs["points"].await_args.args[1] == KAGURA_MEMORIES_COLLECTION
    assert list(chunks.await_args.args[0]) == [loser]
    assert await _queued(world, [loser]) == set()


# ------------------------------------------------- legacy consolidation_task


def _legacy_patches(world: World, stack: ExitStack) -> None:
    async def get_db() -> AsyncIterator[Any]:
        yield world.session

    async def one_page(_: Any) -> AsyncIterator[list[Any]]:
        # Only this test's user: the task otherwise walks every graph row.
        yield [SimpleNamespace(user_id=world.owner)]

    stack.enter_context(patch("tasks.neural_tasks.get_db", get_db))
    stack.enter_context(patch("tasks.neural_tasks._graph_pages", one_page))
    stack.enter_context(
        patch(
            "services.memory_service.resolve_collection_name",
            AsyncMock(return_value=_COLLECTION),
        )
    )


async def test_the_legacy_task_whose_commit_fails_keeps_the_rows_and_their_points(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock
) -> None:
    from tasks.neural_tasks import consolidation_task

    ids = await _old_working(world, 2, importance=0.0)
    with ExitStack() as stack:
        _legacy_patches(world, stack)
        stack.enter_context(_commit_fails(world))
        await consolidation_task()  # logs the failure, never raises
    await world.session.rollback()

    assert await _exist(world, ids) == 2
    client.delete.assert_not_awaited()
    _stubs["points"].assert_not_awaited()
    chunks.assert_not_awaited()
    assert await _queued(world, ids) == set()


async def test_the_legacy_task_deletes_the_points_after_its_commit(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock
) -> None:
    from tasks.neural_tasks import consolidation_task

    ids = await _old_working(world, 2, importance=0.0)
    with ExitStack() as stack:
        _legacy_patches(world, stack)
        await consolidation_task()

    assert await _exist(world, ids) == 0
    client.delete.assert_not_awaited()
    assert _deleted_points(_stubs["points"]) == {str(i) for i in ids}
    # The collection of the row's context, not the default one.
    assert _stubs["points"].await_args.args[1] == _COLLECTION
    assert set(chunks.await_args.args[0]) == set(ids)
    # No foreign key to memories: the hard delete keeps the queue row, and
    # the fast path dequeued it.
    assert await _queued(world, ids) == set()
