"""#3019 (points 3 and 4), on Postgres: Sleep coverage and the re-evaluation clock.

Rows are dated explicitly; ``now`` is taken inside each test. No test sleeps.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from services.memory_listing import changes_since
from services.sleep.importance_reeval import STALENESS_DAYS, ImportanceReevalPhase
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow


async def _scope(db) -> tuple[str, Workspace, Context]:
    owner = f"cov_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"cov-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"cov-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    return owner, ws, ctx


def _memory(owner: str, ws: Workspace, ctx: Context, **fields) -> Memory:
    values = {
        "summary": f"memory {uuid4().hex[:8]}",
        "content": "c",
        "type": "note",
        "importance": 0.5,
        "client": "pytest",
        "scope": "working",
        "embedding_status": "success",
        **fields,
    }
    return Memory(id=uuid4(), workspace_id=ws.id, context_id=ctx.id, user_id=owner, **values)


def _reeval_config():
    from unittest.mock import MagicMock

    config = MagicMock()
    config.sleep_importance_reeval_enabled = True
    config.importance_ema_alpha = 0.3
    config.sleep_llm_provider = "openai"
    config.sleep_llm_model = "m"
    return config


async def _reevaluate(db, owner, ws, ctx, scores=None) -> dict:
    phase = ImportanceReevalPhase(db, AsyncMock())
    phase._chunk_cascade = AsyncMock(return_value=False)

    async def judge(batch, *args, **kwargs):
        # The judge answers; it scores the rows ``scores`` names.
        phase._judged.extend(m.id for m in batch)
        return {m.id: scores[m.id] for m in batch if scores and m.id in scores}

    phase._judged = []
    phase._evaluate_batch = judge
    with patch("services.sleep.importance_reeval.update_memory_payload_in_qdrant", new=AsyncMock()):
        result = await phase.execute(
            _reeval_config(), owner, str(ws.id), str(ctx.id), SleepBudget(max_llm_calls=1)
        )
    await db.flush()
    return {"result": result, "judged": list(phase._judged)}


@pytest.mark.asyncio
async def test_reevaluation_cadence_with_one_call_a_night(db_session):
    """25 due rows, one call a night: 10, 10, 5, then none inside the window;
    a row edited inside the window is not due."""
    owner, ws, ctx = await _scope(db_session)
    old = utcnow() - timedelta(days=STALENESS_DAYS + 30)
    rows = [_memory(owner, ws, ctx, created_at=old + timedelta(minutes=i)) for i in range(25)]
    edited = _memory(owner, ws, ctx, created_at=old, updated_at=utcnow() - timedelta(days=1))
    db_session.add_all([*rows, edited])
    await db_session.flush()

    nights = [await _reevaluate(db_session, owner, ws, ctx) for _ in range(4)]

    assert [len(n["judged"]) for n in nights] == [10, 10, 5, 0]
    assert [n["result"].details.get("more_due") for n in nights[:3]] == [True, True, False]
    judged = [mid for n in nights for mid in n["judged"]]
    assert len(set(judged)) == 25
    assert edited.id not in judged
    # Oldest first: the first night took the ten oldest rows.
    assert nights[0]["judged"] == [m.id for m in rows[:10]]


@pytest.mark.asyncio
async def test_reevaluation_is_not_an_update(db_session):
    """A re-evaluated row keeps ``updated_at`` NULL and changes_since lists no
    ``updated`` event for it."""
    owner, ws, ctx = await _scope(db_session)
    old = utcnow() - timedelta(days=STALENESS_DAYS + 30)
    rescored = _memory(owner, ws, ctx, created_at=old)
    db_session.add(rescored)
    await db_session.flush()
    since = utcnow() - timedelta(minutes=1)

    night = await _reevaluate(db_session, owner, ws, ctx, scores={rescored.id: 0.9})

    assert night["result"].details["updated"] == 1
    row = (
        await db_session.execute(
            select(Memory.importance, Memory.updated_at, Memory.importance_reevaluated_at).where(
                Memory.id == rescored.id
            )
        )
    ).one()
    assert row.importance == pytest.approx(0.3 * 0.9 + 0.7 * 0.5)
    assert row.updated_at is None
    assert row.importance_reevaluated_at is not None
    page = await changes_since(
        db_session, context_id=ctx.id, owner_user_id=None, since=since, until=None
    )
    assert [c for c in page.changes if c.memory_id == rescored.id and c.kind == "updated"] == []


# ---------------------------------------------------------------------------
# Dedup coverage through the orchestrator (rule path, no model)
# ---------------------------------------------------------------------------


class _Partition:
    """A context's memories with one stored vector each, searched in memory.

    The vector store is not needed: the embedding of a row's summary, the
    search and the stored-vector read are faked over this one table, so the
    window, the eligibility check, the member load, the merge and the
    positions all run on Postgres.
    """

    def __init__(self, db, owner, ws, ctx, dims: int = 64) -> None:
        self.db, self.owner, self.ws, self.ctx, self.dims = db, owner, ws, ctx, dims
        self.vectors: dict[str, list[float]] = {}
        self.rows: list[Memory] = []
        self.windows: list[list] = []

    def axis(self, index: int) -> list[float]:
        vector = [0.0] * self.dims
        vector[index] = 1.0
        return vector

    async def add(self, *, created_at, vector, **fields) -> Memory:
        row = _memory(self.owner, self.ws, self.ctx, created_at=created_at, **fields)
        self.db.add(row)
        await self.db.flush()
        self.vectors[str(row.id)] = vector
        self.rows.append(row)
        return row

    def _embedding_service(self):
        from unittest.mock import MagicMock

        by_summary = {row.summary: self.vectors[str(row.id)] for row in self.rows}
        service = MagicMock()
        service.provider = "test"
        service.model = "test"
        service.embed_with_usage = AsyncMock(side_effect=lambda text, **_: (by_summary[text], 1))
        return service

    async def _search(self, *, query_vector, limit, filters, **_):
        hits = []
        for point_id, vector in self.vectors.items():
            score = sum(a * b for a, b in zip(query_vector, vector, strict=True))
            if score >= filters["score_threshold"]:
                hits.append({"id": point_id, "score": score})
        return sorted(hits, key=lambda h: -h["score"])[:limit]

    async def _vectors(self, collection, point_ids):
        return {pid: self.vectors[pid] for pid in point_ids if pid in self.vectors}

    async def run(self, n: int, *, started_at=None, raise_search: bool = False, report=None):
        """One Sleep run of dedup alone, on a report started at ``started_at``
        (or on ``report``, created by the caller, as the manual trigger does)."""
        from contextlib import ExitStack

        from models.sleep import SleepReport
        from neural.config import NeuralMemoryConfig
        from services.sleep import dedup_merge
        from services.sleep.coverage import fetch_window
        from services.sleep.orchestrator import SleepOrchestrator
        from services.sleep.reporter import PhaseResult

        if report is None:
            report = SleepReport(
                user_id=self.owner,
                workspace_id=self.ws.id,
                context_id=self.ctx.id,
                status="running",
                started_at=started_at,
            )
            self.db.add(report)
            await self.db.flush()

        async def recording_fetch(db, eligible, lanes, limit):
            window, coverage = await fetch_window(db, eligible, lanes, limit)
            self.windows.append([m.id for m in window])
            return window, coverage

        config = NeuralMemoryConfig(
            sleep_llm_provider="",
            sleep_dedup_enabled=True,
            sleep_max_memories_per_run=n,
            sleep_dedup_similarity_threshold=0.92,
        )
        with ExitStack() as stack:
            stack.enter_context(patch("services.sleep.orchestrator.FULL_PHASES", {"dedup_merge"}))
            stack.enter_context(
                patch("services.sleep.orchestrator.sleep_suspended", return_value=False)
            )
            stack.enter_context(
                patch("services.sleep.orchestrator.slot_suspended", AsyncMock(return_value=False))
            )
            reindex = stack.enter_context(patch("services.sleep.orchestrator.ReindexPhase"))
            reindex.return_value.execute = AsyncMock(return_value=PhaseResult(phase_name="reindex"))
            stack.enter_context(
                patch.object(
                    dedup_merge, "EmbeddingService", return_value=self._embedding_service()
                )
            )
            stack.enter_context(patch.object(dedup_merge, "search_memories_qdrant", self._search))
            stack.enter_context(patch.object(dedup_merge, "retrieve_dense_vectors", self._vectors))
            stack.enter_context(patch.object(dedup_merge, "fetch_window", recording_fetch))
            if raise_search:
                stack.enter_context(
                    patch.object(
                        dedup_merge.DedupMergePhase,
                        "_find_similar_pairs",
                        AsyncMock(side_effect=RuntimeError("search down")),
                    )
                )
            await SleepOrchestrator(self.db).run(
                self.owner, str(self.ws.id), str(self.ctx.id), config=config, report=report
            )
        await self.db.flush()
        return report


async def _full_context(db) -> tuple[str, Workspace, Context]:
    owner, ws, ctx = await _scope(db)
    ctx.sleep_mode = "full"
    await db.flush()
    return owner, ws, ctx


def _state(report):
    from services.sleep.coverage import parse_state

    return parse_state(report.coverage_state)


@pytest.mark.asyncio
async def test_acceptance_an_old_duplicate_outside_the_window_is_merged(db_session):
    """Partition of 8, allowance 2. Run 1 moves the sweep below the top. A new
    memory then duplicates the OLDEST row: run 2's window is the new row
    (changed lane) and the next sweep row; the old duplicate is in neither, is
    found as the new row's neighbour, passes the eligibility check and loses
    the merge."""
    from models.memory import DELETED_BY_SLEEP_MERGE

    owner, ws, ctx = await _full_context(db_session)
    part = _Partition(db_session, owner, ws, ctx)
    base = utcnow() - timedelta(days=30)
    rows = [
        await part.add(created_at=base + timedelta(days=i), vector=part.axis(i)) for i in range(8)
    ]
    oldest, newest = rows[0], rows[-1]

    first = await part.run(2, started_at=utcnow() - timedelta(hours=2))
    state = _state(first)
    assert part.windows[0] == [newest.id, rows[-2].id]
    assert state.dedup.sweep.position == (rows[-2].created_at, rows[-2].id)

    duplicate = await part.add(
        created_at=utcnow() - timedelta(hours=1), vector=part.axis(0), summary="dup of oldest"
    )
    second = await part.run(2, started_at=utcnow() - timedelta(minutes=30))

    assert part.windows[1] == [duplicate.id, rows[-3].id]
    assert oldest.id not in part.windows[1]
    assert second.memories_merged == 1
    deleted = (
        await db_session.execute(
            select(Memory.id, Memory.deleted_at, Memory.deleted_by).where(
                Memory.id.in_([oldest.id, duplicate.id])
            )
        )
    ).all()
    by_id = {row.id: row for row in deleted}
    assert by_id[oldest.id].deleted_by == DELETED_BY_SLEEP_MERGE
    assert by_id[duplicate.id].deleted_at is None


@pytest.mark.asyncio
async def test_acceptance_every_row_is_read_in_bounded_runs(db_session):
    """Partition of 10, allowance 4 (sweep reserve 1), and 3 new rows before
    every run so the changed lane is full: the sweep still advances exactly
    one row a run, the windows of the first ceil(10 / 1) runs hold all 10
    rows, and the pass is counted by the next run, whose read finds no row
    below the sweep's position (the lane is exhausted)."""
    owner, ws, ctx = await _full_context(db_session)
    part = _Partition(db_session, owner, ws, ctx)
    start = utcnow() - timedelta(days=60)
    original = [
        await part.add(created_at=start + timedelta(days=i), vector=part.axis(i)) for i in range(10)
    ]
    added = 10
    reports = []
    for night in range(11):
        for _ in range(3):
            await part.add(
                created_at=utcnow() - timedelta(days=1) + timedelta(minutes=added),
                vector=part.axis(added),
            )
            added += 1
        reports.append(
            await part.run(4, started_at=utcnow() - timedelta(hours=11) + timedelta(minutes=night))
        )

    sweeps = [r.dedup_result["details"]["coverage"]["sweep"] for r in reports]
    changed = [r.dedup_result["details"]["coverage"]["changed"] for r in reports]
    assert [s["taken"] for s in sweeps[:10]] == [1] * 10
    assert [s["advanced"] for s in sweeps[:10]] == [1] * 10
    assert [c["taken"] for c in changed] == [3] * 11
    first_pass = {mid for window in part.windows[:10] for mid in window}
    assert {m.id for m in original} <= first_pass
    assert [_state(r).dedup.sweep.passes for r in reports] == [0] * 10 + [1]
    assert sweeps[10]["pass_completed"] is True


@pytest.mark.asyncio
async def test_acceptance_reevaluation_changes_neither_lane(db_session):
    """Re-evaluating old rows leaves the dedup window as it was."""
    from services.sleep.coverage import DedupLanes, LaneEntry, changed_key, fetch_window
    from services.sleep.dedup_merge import dedup_eligible

    owner, ws, ctx = await _scope(db_session)
    old = utcnow() - timedelta(days=STALENESS_DAYS + 30)
    rows = [_memory(owner, ws, ctx, created_at=old + timedelta(hours=i)) for i in range(6)]
    db_session.add_all(rows)
    await db_session.flush()
    position = changed_key(rows[2])
    lanes = DedupLanes(changed=LaneEntry(at=position[0], id=position[1]))
    eligible = dedup_eligible(owner, str(ws.id), str(ctx.id))

    before, _ = await fetch_window(db_session, eligible, lanes, 4)
    await _reevaluate(db_session, owner, ws, ctx, scores={m.id: 0.9 for m in rows})
    after, _ = await fetch_window(db_session, eligible, lanes, 4)

    assert [m.id for m in after] == [m.id for m in before]


@pytest.mark.asyncio
async def test_the_lookup_takes_the_latest_report_with_a_state(db_session):
    from models.sleep import SleepReport
    from services.sleep.coverage import (
        CoverageState,
        DedupLanes,
        LaneEntry,
        current_state,
        dump_state,
    )

    owner, ws, ctx = await _scope(db_session)
    other = f"cov_{uuid4().hex[:8]}"
    t0 = utcnow() - timedelta(days=3)

    def state(passes: int):
        return CoverageState(dedup=DedupLanes(sweep=LaneEntry(passes=passes)))

    def report(user, started_at, coverage=None, **fields):
        # Every run completed a few minutes after it started, so a lookup
        # ordered by completed_at would see the rollback below.
        return SleepReport(
            user_id=user,
            workspace_id=ws.id,
            context_id=ctx.id,
            status="completed",
            started_at=started_at,
            completed_at=started_at + timedelta(minutes=5),
            coverage_state=dump_state(coverage) if coverage else None,
            **fields,
        )

    older = report(owner, t0, state(1))
    current = report(owner, t0 + timedelta(hours=1), state(2))
    db_session.add_all(
        [
            older,
            current,
            # Later, but without a state: a skipped run, an analysis row.
            report(owner, t0 + timedelta(hours=2)),
            report(owner, t0 + timedelta(hours=3), source="analysis", paid_by="byok"),
            # Another member of the same context has a state of their own.
            report(other, t0 + timedelta(hours=4), state(9)),
        ]
    )
    await db_session.flush()

    assert (await current_state(db_session, owner, str(ctx.id))).dedup.sweep.passes == 2
    assert (await current_state(db_session, other, str(ctx.id))).dedup.sweep.passes == 9
    assert await current_state(db_session, f"none_{uuid4().hex[:6]}", str(ctx.id)) is None

    # Rolling back the OLDER run rewrites its completed_at (now the latest of
    # all), not started_at: the current state does not change.
    older.status = "rolled_back"
    older.completed_at = utcnow()
    await db_session.flush()
    assert older.completed_at > current.completed_at
    assert (await current_state(db_session, owner, str(ctx.id))).dedup.sweep.passes == 2


@pytest.mark.asyncio
async def test_a_phase_that_raises_stalls_then_passes_its_window(db_session):
    owner, ws, ctx = await _full_context(db_session)
    part = _Partition(db_session, owner, ws, ctx)
    base = utcnow() - timedelta(days=30)
    rows = [
        await part.add(created_at=base + timedelta(days=i), vector=part.axis(i)) for i in range(4)
    ]
    reports = []
    for night in range(3):
        reports.append(
            await part.run(
                2,
                started_at=utcnow() - timedelta(hours=5) + timedelta(minutes=night),
                raise_search=True,
            )
        )
    states = [_state(r) for r in reports]
    assert [s.dedup.sweep.stalls for s in states] == [1, 2, 0]
    assert states[0].dedup.sweep.position is None
    assert states[1].dedup.sweep.position is None
    assert states[2].dedup.sweep.position == (rows[2].created_at, rows[2].id)
    assert states[2].dedup.changed.position == (rows[3].created_at, rows[3].id)
    # The phase never returned: the lanes did not take turns.
    assert {s.dedup.turn for s in states} == {"changed"}
    # Every run stored what it did to the lanes although the phase raised (its
    # point-5 keys stay absent); the third counts the rows its stall passed.
    blobs = [r.dedup_result for r in reports]
    assert [b["error"] for b in blobs] == ["search down"] * 3
    assert [set(b["details"]) for b in blobs] == [{"coverage"}] * 3
    assert [b["details"]["coverage"]["skipped_after_stalls"] for b in blobs] == [0, 0, 2]


@pytest.mark.asyncio
async def test_a_rolled_back_run_keeps_its_positions(db_session):
    """D3: ``rollback_sleep_run`` leaves the positions; it rewrites the report's
    status and completed_at, and the next run continues from that report."""
    owner, ws, ctx = await _full_context(db_session)
    part = _Partition(db_session, owner, ws, ctx)
    base = utcnow() - timedelta(days=30)
    rows = [
        await part.add(created_at=base + timedelta(days=i), vector=part.axis(i)) for i in range(5)
    ]
    first = await part.run(2, started_at=utcnow() - timedelta(hours=3))
    first.status = "rolled_back"
    first.completed_at = utcnow()
    await db_session.flush()

    await part.run(2, started_at=utcnow() - timedelta(hours=2))

    # Nothing changed since run 1; the sweep continues below run 1's row.
    assert part.windows[1] == [rows[2].id, rows[1].id]


@pytest.mark.asyncio
async def test_a_failed_run_leaves_the_previous_state_current(db_session):
    """Nightly shape (the run's transaction rolls back) and manual shape (the
    phases' savepoint rolls back, the report is refreshed and failed): the
    next run reads the state of the run before."""
    from services.sleep.reporter import SleepReporter

    owner, ws, ctx = await _full_context(db_session)
    part = _Partition(db_session, owner, ws, ctx)
    base = utcnow() - timedelta(days=30)
    rows = [
        await part.add(created_at=base + timedelta(days=i), vector=part.axis(i)) for i in range(6)
    ]
    first = await part.run(2, started_at=utcnow() - timedelta(hours=4))
    kept = _state(first)

    # Nightly: the whole run is rolled back.
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            with patch.object(
                SleepReporter, "complete_report", AsyncMock(side_effect=RuntimeError("fatal"))
            ):
                await part.run(2, started_at=utcnow() - timedelta(hours=3))

    # Manual: the report row is created first and stays, failed, with no state.
    from models.sleep import SleepReport

    manual = SleepReport(
        user_id=owner,
        workspace_id=ws.id,
        context_id=ctx.id,
        status="running",
        started_at=utcnow() - timedelta(hours=2),
    )
    db_session.add(manual)
    await db_session.flush()
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            with patch.object(
                SleepReporter, "complete_report", AsyncMock(side_effect=RuntimeError("fatal"))
            ):
                await part.run(2, report=manual)
    await db_session.refresh(manual)
    assert manual.coverage_state is None
    await SleepReporter(db_session).fail_report(manual, "fatal")
    await db_session.flush()

    from services.sleep.coverage import current_state

    assert await current_state(db_session, owner, str(ctx.id)) == kept
    # The next run continues from run 1's positions.
    await part.run(2, started_at=utcnow() - timedelta(hours=1))
    assert part.windows[-1] == [rows[3].id, rows[2].id]


@pytest.mark.asyncio
async def test_with_one_row_a_run_the_lanes_take_turns(db_session):
    from services.sleep.coverage import DedupLanes, LaneEntry, changed_key, fetch_window, sweep_key
    from services.sleep.dedup_merge import dedup_eligible

    owner, ws, ctx = await _scope(db_session)
    base = utcnow() - timedelta(days=30)
    rows = [_memory(owner, ws, ctx, created_at=base + timedelta(days=i)) for i in range(4)]
    db_session.add_all(rows)
    await db_session.flush()
    eligible = dedup_eligible(owner, str(ws.id), str(ctx.id))
    changed_at, sweep_at = changed_key(rows[2]), sweep_key(rows[2])
    lanes = DedupLanes(
        changed=LaneEntry(at=changed_at[0], id=changed_at[1]),
        sweep=LaneEntry(at=sweep_at[0], id=sweep_at[1]),
    )

    on_changed, window = await fetch_window(db_session, eligible, lanes, 1)
    assert [m.id for m in on_changed] == [rows[3].id]
    assert window.sweep == []

    on_sweep, window = await fetch_window(
        db_session, eligible, DedupLanes(changed=lanes.changed, sweep=lanes.sweep, turn="sweep"), 1
    )
    assert [m.id for m in on_sweep] == [rows[1].id]
    assert window.changed == []
