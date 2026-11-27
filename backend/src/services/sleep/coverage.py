"""Coverage positions of the Sleep phases that work through a partition (#3019).

A partition is the rows of one user in one context, what a Sleep run works
on. Dedup takes its window from two lanes and the consolidation judge reads
its borderline rows from one position, so every row is reached in a bounded
number of runs whatever the per-run allowance:

* **changed lane** — rows written or edited since the lane's position, newest
  first when more changed than fit (what does not fit is left to the sweep);
  its rows are then judged oldest first. Keyed on
  ``coalesce(updated_at, created_at), id``.
* **sweep lane** — every row in stable ``(created_at, id)`` order, newest
  first; after the oldest row it wraps and a new pass starts.

The two lane statements order and bound the rows; they gate nothing: the
changed lane reads what changed since its position, the sweep reads every row
on each pass, so together they read every row.

A position moves only over rows whose outcome in the run is ``settled`` or
``passed`` (the prefix rule), never past a row left for budget or held by a
failure, except through the stall rule. The state lives on the run's report
(``sleep_reports.coverage_state``): the orchestrator reads the latest one after
taking the graph learning lock and writes the new one before completing the
report; a phase never writes it. An older state can only make rows be read
again, never skip one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, func, literal, select, tuple_
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from models.memory import Memory
from models.sleep import SleepReport
from utils.logger import get_logger

logger = get_logger(__name__)

STATE_VERSION = 1
# The changed lane's position never comes closer than this to the run's start,
# so a row whose transaction was still open at the read is read again.
CHANGED_LANE_OVERLAP = timedelta(minutes=10)
# Runs in a row a lane may wait on a failure at its head before it moves on.
STALL_LIMIT = 3
NIL_UUID = UUID(int=0)

# Row outcomes: what happened to a window row in one run.
SETTLED = "settled"  # done for this pass (judged, nothing to judge, ...)
PASSED = "passed"  # a failure of its own while the same kind of work succeeded
BUDGET = "budget"  # not reached for lack of calls, or a merge left more to do
FAILURE = "failure"  # nothing of its kind succeeded (or the phase raised)
_MOVES_PAST = frozenset({SETTLED, PASSED})

LANE_CHANGED = "changed"
LANE_SWEEP = "sweep"
_TURNS = (LANE_CHANGED, LANE_SWEEP)

Key = tuple[datetime, UUID]


@dataclass(frozen=True)
class LaneEntry:
    """One lane's position, how many runs it stalled, and (sweep) its passes."""

    at: datetime | None = None
    id: UUID | None = None
    stalls: int = 0
    passes: int = 0

    @property
    def position(self) -> Key | None:
        if self.at is None or self.id is None:
            return None
        return (self.at, self.id)


@dataclass(frozen=True)
class DedupLanes:
    """Dedup's two lanes, and which one gets a bound of 1 next run."""

    changed: LaneEntry = field(default_factory=LaneEntry)
    sweep: LaneEntry = field(default_factory=LaneEntry)
    turn: str = LANE_CHANGED


@dataclass(frozen=True)
class CoverageState:
    """The coverage state of one partition, version 1."""

    dedup: DedupLanes = field(default_factory=DedupLanes)
    judge: LaneEntry = field(default_factory=LaneEntry)


# ---------------------------------------------------------------- state I/O


def _parse_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("position time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _parse_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("count")
    return value


def _parse_entry(raw: Any) -> LaneEntry:
    if not isinstance(raw, dict):
        raise ValueError("entry")
    at = _parse_at(raw.get("at"))
    raw_id = raw.get("id")
    if raw_id is not None and not isinstance(raw_id, str):
        raise ValueError("position id")
    row_id = UUID(raw_id) if raw_id is not None else None
    if (at is None) != (row_id is None):
        raise ValueError("half a position")
    return LaneEntry(
        at=at,
        id=row_id,
        stalls=_parse_count(raw.get("stalls")),
        passes=_parse_count(raw.get("passes")),
    )


def parse_state(value: Any) -> CoverageState | None:
    """Read a stored ``coverage_state``.

    Anything that is not a dict with ``v == 1`` is no state (None). An entry
    that is missing or does not parse starts from the beginning; the others
    are kept. Logged once, with no content.
    """
    if not isinstance(value, dict) or value.get("v") != STATE_VERSION:
        if value is not None:
            logger.warning("sleep_coverage_state_ignored", reason="not_version_1")
        return None
    unreadable: list[str] = []

    def entry(container: Any, name: str, label: str) -> LaneEntry:
        raw = container.get(name) if isinstance(container, dict) else None
        if raw is None:
            return LaneEntry()
        try:
            return _parse_entry(raw)
        except (ValueError, TypeError):
            unreadable.append(label)
            return LaneEntry()

    dedup_raw = value.get("dedup_merge")
    turn = dedup_raw.get("turn") if isinstance(dedup_raw, dict) else None
    state = CoverageState(
        dedup=DedupLanes(
            changed=entry(dedup_raw, LANE_CHANGED, "dedup_merge.changed"),
            sweep=entry(dedup_raw, LANE_SWEEP, "dedup_merge.sweep"),
            turn=turn if turn in _TURNS else LANE_CHANGED,
        ),
        judge=entry(value.get("consolidation"), "judge", "consolidation.judge"),
    )
    if unreadable:
        logger.warning("sleep_coverage_state_entries_reset", entries=unreadable)
    return state


def _dump_entry(entry: LaneEntry, *, passes: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "at": entry.at.isoformat() if entry.at is not None else None,
        "id": str(entry.id) if entry.id is not None else None,
        "stalls": entry.stalls,
    }
    if passes:
        out["passes"] = entry.passes
    return out


def dump_state(state: CoverageState) -> dict[str, Any]:
    """The JSON form of ``state`` (version 1)."""
    return {
        "v": STATE_VERSION,
        "dedup_merge": {
            LANE_CHANGED: _dump_entry(state.dedup.changed, passes=False),
            LANE_SWEEP: _dump_entry(state.dedup.sweep, passes=True),
            "turn": state.dedup.turn,
        },
        "consolidation": {"judge": _dump_entry(state.judge, passes=False)},
    }


async def current_state(
    db: AsyncSession, user_id: str, context_id: str | None
) -> CoverageState | None:
    """The state of the partition's latest report that has one.

    "Latest" is the greatest ``(started_at, id)``: ``rollback_sleep_run``
    rewrites ``completed_at``, not ``started_at``. Called after the graph
    learning lock, which serialises the runs of one user, so the previous
    run's commit is visible.
    """
    stmt = (
        select(SleepReport.coverage_state)
        .where(
            SleepReport.user_id == user_id,
            SleepReport.coverage_state.is_not(None),
            (
                SleepReport.context_id == UUID(context_id)
                if context_id
                else SleepReport.context_id.is_(None)
            ),
        )
        .order_by(SleepReport.started_at.desc(), SleepReport.id.desc())
        .limit(1)
    )
    return parse_state((await db.execute(stmt)).scalar_one_or_none())


# ------------------------------------------------------------ shares of a bound


def _turn_share(bound: int, turn: str) -> int:
    """The part of a per-run bound the sweep cannot lose.

    A quarter (at least 1) for a bound of 2 or more; for a bound of 1 the two
    lanes take turns.
    """
    if bound >= 2:
        return max(1, bound // 4)
    if bound == 1:
        return 1 if turn == LANE_SWEEP else 0
    return 0


def reserve_rows(n: int, turn: str) -> int:
    """Window rows reserved for the sweep lane."""
    return _turn_share(n, turn)


def sweep_call_share(calls: int, turn: str) -> int:
    """Judge calls reserved for the sweep lane's clusters."""
    return _turn_share(calls, turn)


# ------------------------------------------------------------------- window


def changed_key(memory: Memory) -> Key:
    """The changed lane's key of a row: ``coalesce(updated_at, created_at), id``."""
    stamp = memory.updated_at if memory.updated_at is not None else memory.created_at
    return (stamp, memory.id)


def sweep_key(memory: Memory) -> Key:
    """The sweep lane's key of a row: ``(created_at, id)``."""
    return (memory.created_at, memory.id)


def _key_literal(key: Key) -> Any:
    return tuple_(literal(key[0], type_=DateTime()), literal(key[1], type_=PG_UUID(as_uuid=True)))


@dataclass
class DedupCoverage:
    """The dedup window of one run and each window row's outcome.

    ``changed`` holds the changed lane's keys in lane order (ascending),
    ``sweep`` the sweep lane's (descending). ``floor`` is the key of the newest
    changed row that did not fit (a backlog), ``exhausted`` that the sweep
    returned fewer rows than its limit. Every outcome starts as ``failure``:
    a phase that raises after the fetch moves no position.
    """

    changed: list[Key]
    sweep: list[Key]
    floor: Key | None
    exhausted: bool
    outcomes: dict[UUID, str] = field(default_factory=dict)

    def lane_rows(self, lane: str, *, returned: bool = True) -> list[tuple[Key, str]]:
        """The lane's rows in lane order with their outcomes; every row is a
        failure when the phase did not return (its savepoint rolled back)."""
        keys = self.changed if lane == LANE_CHANGED else self.sweep
        if not returned:
            return [(key, FAILURE) for key in keys]
        return [(key, self.outcomes.get(key[1], FAILURE)) for key in keys]


@dataclass
class JudgeCoverage:
    """The consolidation judge's rows of one run, in the order it saw them,
    and each row's outcome (``failure`` until the pass finished)."""

    keys: list[Key]
    outcomes: dict[UUID, str] = field(default_factory=dict)

    def rows(self, *, returned: bool = True) -> list[tuple[Key, str]]:
        if not returned:
            return [(key, FAILURE) for key in self.keys]
        return [(key, self.outcomes.get(key[1], FAILURE)) for key in self.keys]


def rotate(memories: list[Memory], position: Key | None) -> list[Memory]:
    """``memories`` in ``(created_at, id)`` order, starting after ``position``
    and wrapping (from the oldest when ``position`` is None)."""
    ordered = sorted(memories, key=sweep_key)
    if position is None:
        return ordered
    return [m for m in ordered if sweep_key(m) > position] + [
        m for m in ordered if sweep_key(m) <= position
    ]


async def fetch_window(
    db: AsyncSession,
    eligible: ColumnElement[bool],
    lanes: DedupLanes,
    n: int,
) -> tuple[list[Memory], DedupCoverage]:
    """Read dedup's window: the changed lane, then the sweep lane.

    ``eligible`` is the WHERE every dedup read uses; the two statements here
    only order and bound the rows. The changed lane takes the newest ``k``
    rows changed since its position (one more row tells whether there is a
    backlog); the sweep takes the rest of the ``n`` rows below its position,
    newest first, and is exhausted when it returns fewer than it asked for.
    Row-value comparisons, so rows with equal timestamps neither repeat nor
    skip.
    """
    reserve = reserve_rows(n, lanes.turn)
    k = n - reserve
    changed_rows: list[Memory] = []
    floor: Key | None = None
    if k > 0:
        stamp = func.coalesce(Memory.updated_at, Memory.created_at)
        stmt = select(Memory).where(eligible)
        if lanes.changed.position is not None:
            stmt = stmt.where(tuple_(stamp, Memory.id) > _key_literal(lanes.changed.position))
        stmt = stmt.order_by(stamp.desc(), Memory.id.desc()).limit(k + 1)
        rows = list((await db.execute(stmt)).scalars().all())
        if len(rows) > k:
            floor = changed_key(rows[k])
            rows = rows[:k]
        changed_rows = list(reversed(rows))

    sweep_rows: list[Memory] = []
    exhausted = False
    limit = n - len(changed_rows)
    if limit > 0:
        stmt = select(Memory).where(eligible)
        if changed_rows:
            stmt = stmt.where(Memory.id.not_in([m.id for m in changed_rows]))
        if lanes.sweep.position is not None:
            stmt = stmt.where(
                tuple_(Memory.created_at, Memory.id) < _key_literal(lanes.sweep.position)
            )
        stmt = stmt.order_by(Memory.created_at.desc(), Memory.id.desc()).limit(limit)
        sweep_rows = list((await db.execute(stmt)).scalars().all())
        exhausted = len(sweep_rows) < limit

    window = changed_rows + sweep_rows
    coverage = DedupCoverage(
        changed=[changed_key(m) for m in changed_rows],
        sweep=[sweep_key(m) for m in sweep_rows],
        floor=floor,
        exhausted=exhausted,
        outcomes=dict.fromkeys((m.id for m in window), FAILURE),
    )
    return window, coverage


# --------------------------------------------------------------- positions


@dataclass(frozen=True)
class LanePlan:
    """A lane's new entry and what the run did to it."""

    entry: LaneEntry
    taken: int = 0
    advanced: int = 0
    passed_with_failure: int = 0
    skipped_after_stalls: int = 0
    pass_completed: bool = False


def _prefix_len(rows: list[tuple[Key, str]]) -> int:
    count = 0
    for _key, outcome in rows:
        if outcome not in _MOVES_PAST:
            break
        count += 1
    return count


def _apply_stall_rule(
    entry: LaneEntry, rows: list[tuple[Key, str]]
) -> tuple[list[tuple[Key, str]], int, int]:
    """The rows to plan with, the new stall count, and the rows passed by it.

    A lane whose prefix is empty and whose head row is a ``failure`` stalls;
    on the run that brings the count to ``STALL_LIMIT`` its failure rows count
    as passed for this run. A non-empty prefix resets the count; a head row
    left for budget keeps it.
    """
    if not rows:
        return rows, entry.stalls, 0
    if _prefix_len(rows) > 0:
        return rows, 0, 0
    if rows[0][1] != FAILURE:
        return rows, entry.stalls, 0
    stalls = entry.stalls + 1
    if stalls < STALL_LIMIT:
        return rows, stalls, 0
    passed = [(key, PASSED if outcome == FAILURE else outcome) for key, outcome in rows]
    skipped = sum(1 for _key, before in rows[: _prefix_len(passed)] if before == FAILURE)
    return passed, 0, skipped


def _max_key(a: Key | None, b: Key | None) -> Key | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _min_key(a: Key | None, b: Key | None) -> Key | None:
    if a is None or b is None:
        return None
    return min(a, b)


def plan_changed(
    entry: LaneEntry,
    rows: list[tuple[Key, str]],
    *,
    floor: Key | None,
    now: datetime,
) -> LanePlan:
    """The changed lane's next entry.

    ``rows`` are the lane's rows in lane order (ascending) with their outcomes.
    The position moves to the last row of the settled-or-passed prefix — or,
    when the lane had a backlog, at least to its floor (what is at or below it
    is the sweep's) — but never closer than ``CHANGED_LANE_OVERLAP`` to the
    run's start, and never back.
    """
    if not rows:
        return LanePlan(entry=entry)
    passed_with_failure = sum(1 for _key, outcome in rows if outcome == PASSED)
    planned, stalls, skipped = _apply_stall_rule(entry, rows)
    prefix = _prefix_len(planned)
    old = entry.position
    base = _max_key(old, floor) if floor is not None else old
    end = planned[prefix - 1][0] if prefix else base
    clamp: Key = (now - CHANGED_LANE_OVERLAP, NIL_UUID)
    new = _max_key(old, _min_key(end, clamp))
    advanced = sum(1 for key, _outcome in rows if new is not None and key <= new)
    return LanePlan(
        entry=replace(
            entry,
            at=new[0] if new is not None else None,
            id=new[1] if new is not None else None,
            stalls=stalls,
        ),
        taken=len(rows),
        advanced=advanced,
        passed_with_failure=passed_with_failure,
        skipped_after_stalls=skipped,
    )


def plan_sweep(
    entry: LaneEntry,
    rows: list[tuple[Key, str]],
    *,
    exhausted: bool,
    count_passes: bool = True,
) -> LanePlan:
    """A sweeping lane's next entry.

    ``rows`` are the lane's rows in lane order with their outcomes. The
    position moves to the last row of the settled-or-passed prefix. When the
    lane was exhausted and every row it took is in the prefix (or it took none
    while its position was set) the pass is complete: the position goes back to
    the start and, with ``count_passes``, ``passes`` grows by one.
    """
    passed_with_failure = sum(1 for _key, outcome in rows if outcome == PASSED)
    planned, stalls, skipped = _apply_stall_rule(entry, rows)
    prefix = _prefix_len(planned)
    position = planned[prefix - 1][0] if prefix else entry.position
    completed = (
        exhausted and prefix == len(planned) and (bool(planned) or entry.position is not None)
    )
    passes = entry.passes
    if completed:
        position = None
        if count_passes:
            passes += 1
    return LanePlan(
        entry=replace(
            entry,
            at=position[0] if position is not None else None,
            id=position[1] if position is not None else None,
            stalls=stalls,
            passes=passes,
        ),
        taken=len(rows),
        advanced=prefix,
        passed_with_failure=passed_with_failure,
        skipped_after_stalls=skipped,
        pass_completed=completed,
    )


def advance_dedup(
    lanes: DedupLanes,
    coverage: DedupCoverage | None,
    *,
    returned: bool,
    now: datetime,
) -> tuple[DedupLanes, dict[str, Any] | None]:
    """Dedup's next lanes and the ``details["coverage"]`` report of the run.

    No window (the phase was skipped, or raised before its fetch): the lanes
    are carried as they are. A phase that did not return moves no position:
    every row of its window counts as a failure (the stall rule applies), and
    a changed-lane backlog is not handed to the sweep (the position does not
    jump to its floor; the next run finds the same backlog). ``turn`` flips
    after every run in which the phase returned.
    """
    if coverage is None:
        return lanes, None
    floor = coverage.floor if returned else None
    changed = plan_changed(
        lanes.changed,
        coverage.lane_rows(LANE_CHANGED, returned=returned),
        floor=floor,
        now=now,
    )
    sweep = plan_sweep(
        lanes.sweep,
        coverage.lane_rows(LANE_SWEEP, returned=returned),
        exhausted=coverage.exhausted,
    )
    turn = lanes.turn
    if returned:
        turn = LANE_SWEEP if turn == LANE_CHANGED else LANE_CHANGED
    report = {
        LANE_CHANGED: {
            "taken": changed.taken,
            "advanced": changed.advanced,
            "backlog_left_to_sweep": floor is not None,
            "passed_with_failure": changed.passed_with_failure,
        },
        LANE_SWEEP: {
            "taken": sweep.taken,
            "advanced": sweep.advanced,
            "pass_completed": sweep.pass_completed,
            "passes": sweep.entry.passes,
            "passed_with_failure": sweep.passed_with_failure,
        },
        "skipped_after_stalls": changed.skipped_after_stalls + sweep.skipped_after_stalls,
    }
    if report["skipped_after_stalls"]:
        logger.info(
            "sleep_coverage_passed_after_stalls",
            phase="dedup_merge",
            rows=report["skipped_after_stalls"],
        )
    return DedupLanes(changed=changed.entry, sweep=sweep.entry, turn=turn), report


def advance_judge(
    entry: LaneEntry,
    coverage: JudgeCoverage | None,
    *,
    returned: bool,
) -> tuple[LaneEntry, dict[str, Any] | None]:
    """The consolidation judge's next position and its ``details["coverage"]``.

    The rows it saw this run, in that order, are planned like a sweep that is
    always exhausted: the position moves to the last settled-or-passed row,
    or back to the start when every borderline row was judged. No pass count.
    """
    if coverage is None:
        return entry, None
    plan = plan_sweep(entry, coverage.rows(returned=returned), exhausted=True, count_passes=False)
    if plan.skipped_after_stalls:
        logger.info(
            "sleep_coverage_passed_after_stalls",
            phase="consolidation",
            rows=plan.skipped_after_stalls,
        )
    return plan.entry, {"judge": {"taken": plan.taken, "advanced": plan.advanced}}
