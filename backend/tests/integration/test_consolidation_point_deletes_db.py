"""Consolidation keeps memories; merge point deletes after the commit (#2565, #3021).

#3021: a memory is kept until its owner or writer deletes it. A working row
30 days old or more that no recall ever returned survives a Sleep run (Sleep
on) and the legacy consolidation task (Sleep off), and nothing of it is queued
for a point delete.

The Sleep merge's loser follows ``forget`` (#2476, #2480): its point is queued
in ``deferred_point_deletes`` in the transaction that removes its row, and the
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
from models.memory import DELETED_BY_SLEEP_MERGE, Memory
from models.sleep import SleepReport
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
    world: World,
    count: int,
    *,
    importance: float = 0.1,
    summary: str = "old working memory",
    age_days: int = 40,
) -> list[uuid.UUID]:
    """Working rows nobody adopted or read, ``age_days`` old (what the removed
    archive rules took)."""
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
                created_at=utcnow() - timedelta(days=age_days),
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


def _sleep_patches(world: World, stack: ExitStack, phases: set[str]) -> None:
    """Run only ``phases`` on the caller's session; an old deployment's archive
    cutoff setting is still in the environment (nothing reads it)."""

    async def get_db() -> AsyncIterator[Any]:
        yield world.session

    graph = MagicMock()
    graph.stats = AsyncMock(return_value={"total_edges": 0})
    graph.get_node_metrics = AsyncMock(return_value=None)
    stack.enter_context(patch("db.base.get_db", get_db))
    stack.enter_context(patch("services.sleep.orchestrator.FULL_PHASES", phases))
    stack.enter_context(patch("services.sleep.orchestrator.sleep_suspended", return_value=False))
    stack.enter_context(patch("services.sleep.consolidation.GraphService", return_value=graph))
    stack.enter_context(
        patch.dict("os.environ", {"CONSOLIDATION_ADOPTION_DELETE_CUTOFF": "2000-01-01T00:00:00"})
    )


async def _run_sleep(
    world: World, phases: str | set[str] = "consolidation", *, commit_fails: bool = False
) -> uuid.UUID:
    """The admin trigger's job: the orchestrator, then the caller's commit."""
    from api.routes.admin_sleep import _run_sleep_job

    report_id = await _sleep_report(world)
    with ExitStack() as stack:
        _sleep_patches(world, stack, {phases} if isinstance(phases, str) else phases)
        if commit_fails:
            stack.enter_context(_commit_fails(world))
        # Logs a failure and marks the report, never raises.
        await _run_sleep_job(report_id, world.owner, str(world.workspace_id), str(world.context_id))
    return report_id


@pytest.mark.parametrize(
    "phases",
    [
        {"consolidation"},
        # The non-LLM phases of full mode next to it (the code defaults keep
        # edge discovery and importance re-evaluation off; dedup needs a
        # vector store and is covered by the merge tests below).
        {"consolidation", "merge_retention", "forget_retention", "measurement_retention"},
    ],
)
async def test_a_sleep_run_keeps_an_old_never_returned_working_memory(
    world: World,
    _stubs: dict[str, Any],
    chunks: AsyncMock,
    client: MagicMock,
    phases: set[str],
) -> None:
    """#3021 acceptance, Sleep on: 30 and 400 days old, never read, never
    adopted — kept, working, with nothing queued for its point."""
    ids = await _old_working(world, 1, age_days=30) + await _old_working(world, 1, age_days=400)
    report_id = await _run_sleep(world, phases)

    report = await world.session.get(SleepReport, report_id, populate_existing=True)
    assert report is not None and report.status == "completed"
    assert report.consolidation_result is not None
    details = report.consolidation_result["details"]
    assert (details["rule_deleted"], details["llm_archived"], details["borderline"]) == (0, 0, 2)
    rows = (
        await world.session.execute(
            select(Memory.deleted_at, Memory.scope).where(Memory.id.in_(ids))
        )
    ).all()
    assert [(row.deleted_at, row.scope) for row in rows] == [(None, "working")] * 2
    client.delete.assert_not_awaited()
    _stubs["points"].assert_not_awaited()
    chunks.assert_not_awaited()
    assert await _queued(world, ids) == set()


async def test_a_failed_sleep_phase_takes_its_queue_rows_down_with_its_savepoint(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock
) -> None:
    """A phase that queued a point and then failed (a merge here) leaves the
    row live and the queue empty: its savepoint takes both down."""
    from repositories.memory import MemoryRepository
    from services.deferred_point_deletes import queue_point_deletes

    [memory_id] = await _old_working(world, 1)
    orchestrator = SleepOrchestrator(world.session)

    class _TombstoneThenFail:
        """A phase that tombstones a row, queues its point, then raises."""

        def __init__(self) -> None:
            self.pending_point_deletes: list[Any] = []

        async def execute(self, *_: Any, **__: Any) -> PhaseResult:
            assert await MemoryRepository(world.session).soft_delete(
                memory_id, deleted_by=DELETED_BY_SLEEP_MERGE
            )
            entry = (memory_id, memory_id, KAGURA_MEMORIES_COLLECTION)
            await queue_point_deletes(world.session, [entry])
            self.pending_point_deletes.append(entry)
            assert await _queued(world, [memory_id]) == {memory_id}
            raise RuntimeError("phase failed")

    phase = _TombstoneThenFail()
    result = await orchestrator._run_phase(
        "dedup_merge",
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


@pytest.mark.parametrize("age_days", [30, 400])
async def test_the_legacy_task_keeps_an_old_never_returned_working_memory(
    world: World,
    _stubs: dict[str, Any],
    chunks: AsyncMock,
    client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    age_days: int,
) -> None:
    """#3021 acceptance, Sleep off: the legacy task keeps the row (it used to
    delete it physically), and still promotes a used one by rule."""
    from tasks.neural_tasks import consolidation_task

    monkeypatch.setenv("SLEEP_ENABLED", "false")
    ids = await _old_working(world, 2, importance=0.0, age_days=age_days)
    [used] = await _old_working(world, 1, importance=0.0, age_days=age_days, summary="used")
    await world.session.execute(
        Memory.__table__.update().where(Memory.id == used).values(access_count=1)
    )
    await world.session.commit()
    with ExitStack() as stack:
        _legacy_patches(world, stack)
        result = await consolidation_task()

    assert result is not None and result["deleted"] == 0 and result["promoted"] == 1
    assert await _live(world, ids) == 2
    promoted = await world.session.get(Memory, used, populate_existing=True)
    assert promoted is not None and promoted.scope == "persistent"
    client.delete.assert_not_awaited()
    _stubs["points"].assert_not_awaited()
    chunks.assert_not_awaited()
    assert await _queued(world, ids) == set()
