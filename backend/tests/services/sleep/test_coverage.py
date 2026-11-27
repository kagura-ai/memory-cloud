"""#3019: the pure parts of Sleep coverage — positions, stalls, shares, state."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from services.sleep.coverage import (
    BUDGET,
    CHANGED_LANE_OVERLAP,
    FAILURE,
    NIL_UUID,
    PASSED,
    SETTLED,
    STALL_LIMIT,
    CoverageState,
    DedupCoverage,
    DedupLanes,
    LaneEntry,
    advance_dedup,
    dump_state,
    parse_state,
    plan_changed,
    plan_sweep,
    reserve_rows,
    sweep_call_share,
)

NOW = datetime(2026, 11, 27, 3, 0, 0)
T0 = datetime(2026, 1, 1)


def _keys(count: int, *, start: datetime = T0, step: timedelta = timedelta(hours=1)):
    """Ascending keys, distinct timestamps and ids."""
    return [(start + i * step, UUID(int=i + 1)) for i in range(count)]


def _rows(keys, *outcomes):
    return list(zip(keys, outcomes, strict=True))


def _entry(key=None, **kw) -> LaneEntry:
    return LaneEntry(at=key[0] if key else None, id=key[1] if key else None, **kw)


class TestPlanChanged:
    def test_all_settled_moves_to_the_last_row(self):
        keys = _keys(3)
        plan = plan_changed(_entry(), _rows(keys, SETTLED, SETTLED, SETTLED), floor=None, now=NOW)
        assert plan.entry.position == keys[2]
        assert (plan.taken, plan.advanced, plan.entry.stalls) == (3, 3, 0)

    def test_budget_in_the_middle_stops_the_prefix(self):
        keys = _keys(3)
        plan = plan_changed(_entry(), _rows(keys, SETTLED, BUDGET, SETTLED), floor=None, now=NOW)
        assert plan.entry.position == keys[0]
        assert plan.advanced == 1

    def test_head_budget_keeps_position_and_stalls(self):
        keys = _keys(2)
        old = (T0 - timedelta(days=1), uuid4())
        plan = plan_changed(
            _entry(old, stalls=1), _rows(keys, BUDGET, SETTLED), floor=None, now=NOW
        )
        assert plan.entry.position == old
        assert plan.entry.stalls == 1

    @pytest.mark.parametrize("stalls_before", [0, 1])
    def test_head_failure_stalls(self, stalls_before):
        keys = _keys(2)
        plan = plan_changed(
            _entry(stalls=stalls_before), _rows(keys, FAILURE, SETTLED), floor=None, now=NOW
        )
        assert plan.entry.position is None
        assert plan.entry.stalls == stalls_before + 1
        assert plan.skipped_after_stalls == 0

    def test_third_failure_passes_the_failed_rows(self):
        keys = _keys(3)
        plan = plan_changed(
            _entry(stalls=STALL_LIMIT - 1),
            _rows(keys, FAILURE, FAILURE, BUDGET),
            floor=None,
            now=NOW,
        )
        assert plan.entry.position == keys[1]
        assert plan.entry.stalls == 0
        assert plan.skipped_after_stalls == 2

    def test_head_passed_then_settled_advances_past_all(self):
        keys = _keys(3)
        plan = plan_changed(_entry(), _rows(keys, PASSED, SETTLED, SETTLED), floor=None, now=NOW)
        assert plan.entry.position == keys[2]
        assert plan.passed_with_failure == 1
        assert plan.entry.stalls == 0

    def test_all_failure_is_a_stall(self):
        keys = _keys(2)
        plan = plan_changed(_entry(), _rows(keys, FAILURE, FAILURE), floor=None, now=NOW)
        assert plan.entry.position is None
        assert plan.entry.stalls == 1

    def test_phase_raised_three_times(self):
        keys = _keys(2)
        entry = _entry()
        for run in range(1, STALL_LIMIT + 1):
            plan = plan_changed(entry, _rows(keys, FAILURE, FAILURE), floor=None, now=NOW)
            entry = plan.entry
            if run < STALL_LIMIT:
                assert (entry.position, entry.stalls) == (None, run)
        assert entry.position == keys[1]
        assert entry.stalls == 0
        assert plan.skipped_after_stalls == 2

    def test_nothing_taken_is_unchanged(self):
        old = _entry(_keys(1)[0], stalls=2)
        assert plan_changed(old, [], floor=None, now=NOW).entry == old

    def test_clamp_keeps_the_last_minutes_for_the_next_run(self):
        recent = [(NOW - timedelta(minutes=2), uuid4())]
        plan = plan_changed(_entry(), _rows(recent, SETTLED), floor=None, now=NOW)
        assert plan.entry.position == (NOW - CHANGED_LANE_OVERLAP, NIL_UUID)
        assert plan.advanced == 0

    def test_an_old_position_newer_than_the_clamp_never_moves_back(self):
        old = (NOW - timedelta(minutes=1), uuid4())
        rows = _rows([(NOW - timedelta(seconds=30), uuid4())], SETTLED)
        plan = plan_changed(_entry(old), rows, floor=None, now=NOW)
        assert plan.entry.position == old

    def test_backlog_jumps_to_the_floor_with_an_empty_prefix(self):
        keys = _keys(4)
        floor = keys[1]
        taken = keys[2:]
        plan = plan_changed(_entry(), _rows(taken, BUDGET, BUDGET), floor=floor, now=NOW)
        assert plan.entry.position == floor
        assert plan.advanced == 0

    def test_backlog_with_a_prefix_moves_past_it(self):
        keys = _keys(4)
        plan = plan_changed(_entry(), _rows(keys[2:], SETTLED, BUDGET), floor=keys[1], now=NOW)
        assert plan.entry.position == keys[2]


class TestPlanSweep:
    def test_all_settled_moves_to_the_last_row(self):
        keys = list(reversed(_keys(3)))
        plan = plan_sweep(_entry(), _rows(keys, SETTLED, SETTLED, SETTLED), exhausted=False)
        assert plan.entry.position == keys[2]
        assert not plan.pass_completed

    def test_exhausted_and_all_settled_wraps(self):
        keys = list(reversed(_keys(2)))
        plan = plan_sweep(_entry(keys[0], passes=2), _rows(keys, SETTLED, PASSED), exhausted=True)
        assert plan.entry.position is None
        assert plan.entry.passes == 3
        assert plan.pass_completed

    def test_exhausted_with_an_unsettled_row_does_not_wrap(self):
        keys = list(reversed(_keys(2)))
        plan = plan_sweep(_entry(), _rows(keys, SETTLED, BUDGET), exhausted=True)
        assert plan.entry.position == keys[0]
        assert plan.entry.passes == 0
        assert not plan.pass_completed

    def test_exhausted_with_no_row_wraps_when_a_position_was_set(self):
        old = _keys(1)[0]
        plan = plan_sweep(_entry(old), [], exhausted=True)
        assert plan.entry.position is None
        assert plan.entry.passes == 1

    def test_no_row_and_no_position_is_no_pass(self):
        plan = plan_sweep(_entry(), [], exhausted=True)
        assert plan.entry == LaneEntry()
        assert not plan.pass_completed

    def test_not_exhausted_and_no_row_is_unchanged(self):
        old = _entry(_keys(1)[0], stalls=1, passes=4)
        assert plan_sweep(old, [], exhausted=False).entry == old

    def test_passes_not_counted_when_asked(self):
        keys = _keys(1)
        plan = plan_sweep(_entry(), _rows(keys, SETTLED), exhausted=True, count_passes=False)
        assert plan.pass_completed
        assert plan.entry.passes == 0

    def test_failure_head_stalls_then_passes(self):
        keys = list(reversed(_keys(2)))
        entry = _entry()
        for _ in range(STALL_LIMIT):
            plan = plan_sweep(entry, _rows(keys, FAILURE, SETTLED), exhausted=False)
            entry = plan.entry
        assert entry.position == keys[1]
        assert plan.skipped_after_stalls == 1


class TestShares:
    @pytest.mark.parametrize(
        ("n", "turn", "reserve"),
        [
            (1, "changed", 0),
            (1, "sweep", 1),
            (2, "changed", 1),
            (4, "sweep", 1),
            (200, "changed", 50),
        ],
    )
    def test_reserve_rows(self, n, turn, reserve):
        assert reserve_rows(n, turn) == reserve

    @pytest.mark.parametrize(
        ("calls", "turn", "share"),
        [
            (0, "sweep", 0),
            (1, "changed", 0),
            (1, "sweep", 1),
            (2, "changed", 1),
            (4, "changed", 1),
            (50, "sweep", 12),
        ],
    )
    def test_sweep_call_share(self, calls, turn, share):
        assert sweep_call_share(calls, turn) == share


class TestState:
    def test_round_trip(self):
        state = CoverageState(
            dedup=DedupLanes(
                changed=LaneEntry(at=datetime(2026, 3, 1, 12, 0, 0, 123456), id=uuid4(), stalls=1),
                sweep=LaneEntry(at=T0, id=uuid4(), stalls=2, passes=5),
                turn="sweep",
            ),
            judge=LaneEntry(at=T0, id=uuid4()),
        )
        assert parse_state(dump_state(state)) == state

    def test_the_default_state_round_trips(self):
        assert parse_state(dump_state(CoverageState())) == CoverageState()

    @pytest.mark.parametrize("value", [None, "x", 3, [], {"v": 2}, {"dedup_merge": {}}])
    def test_not_a_version_1_dict_is_no_state(self, value):
        assert parse_state(value) is None

    def test_a_mock_is_no_state(self):
        assert parse_state(MagicMock()) is None

    def test_a_missing_phase_entry_starts_from_the_beginning(self):
        judge_id = uuid4()
        state = parse_state(
            {"v": 1, "consolidation": {"judge": {"at": T0.isoformat(), "id": str(judge_id)}}}
        )
        assert state == CoverageState(judge=LaneEntry(at=T0, id=judge_id))

    def test_garbage_entries_reset_only_themselves(self):
        sweep_id = uuid4()
        state = parse_state(
            {
                "v": 1,
                "dedup_merge": {
                    "changed": {"at": "not a time", "id": "x"},
                    "sweep": {"at": T0.isoformat(), "id": str(sweep_id), "passes": 2},
                    "turn": "elsewhere",
                },
                "consolidation": {"judge": {"at": T0.isoformat(), "id": None}},
            }
        )
        assert state is not None
        assert state.dedup.changed == LaneEntry()
        assert state.dedup.sweep == LaneEntry(at=T0, id=sweep_id, passes=2)
        assert state.dedup.turn == "changed"
        assert state.judge == LaneEntry()


class TestAdvanceDedup:
    def test_no_window_carries_the_lanes(self):
        lanes = DedupLanes(changed=_entry(_keys(1)[0]), turn="sweep")
        assert advance_dedup(lanes, None, returned=True, now=NOW) == (lanes, None)

    def test_turn_flips_when_the_phase_returned(self):
        coverage = DedupCoverage(changed=[], sweep=[], floor=None, exhausted=False)
        lanes, _ = advance_dedup(DedupLanes(), coverage, returned=True, now=NOW)
        assert lanes.turn == "sweep"
        lanes, _ = advance_dedup(lanes, coverage, returned=False, now=NOW)
        assert lanes.turn == "sweep"

    def test_a_phase_that_did_not_return_moves_nothing(self):
        """Invariant 1, held structurally: settled outcomes on the window of a
        phase whose savepoint rolled back (it raised after deciding them) count
        as failures — the lanes stall instead of moving past work that is gone."""
        changed = _keys(1)
        sweep = [(T0 - timedelta(days=5), uuid4())]
        coverage = DedupCoverage(
            changed=changed,
            sweep=sweep,
            floor=None,
            exhausted=True,
            outcomes={changed[0][1]: SETTLED, sweep[0][1]: SETTLED},
        )
        lanes, report = advance_dedup(DedupLanes(), coverage, returned=False, now=NOW)
        assert lanes.changed == LaneEntry(stalls=1)
        assert lanes.sweep == LaneEntry(stalls=1)
        assert report["sweep"]["pass_completed"] is False

    def test_report(self):
        changed = _keys(2)
        sweep = [(T0 - timedelta(days=10 - i), uuid4()) for i in range(2)][::-1]
        coverage = DedupCoverage(
            changed=changed,
            sweep=sweep,
            floor=(T0 - timedelta(days=1), uuid4()),
            exhausted=True,
            outcomes={
                changed[0][1]: SETTLED,
                changed[1][1]: PASSED,
                sweep[0][1]: SETTLED,
                sweep[1][1]: SETTLED,
            },
        )
        lanes, report = advance_dedup(DedupLanes(), coverage, returned=True, now=NOW)
        assert lanes.changed.position == changed[1]
        assert lanes.sweep.position is None
        assert report == {
            "changed": {
                "taken": 2,
                "advanced": 2,
                "backlog_left_to_sweep": True,
                "passed_with_failure": 1,
            },
            "sweep": {
                "taken": 2,
                "advanced": 2,
                "pass_completed": True,
                "passes": 1,
                "passed_with_failure": 0,
            },
            "skipped_after_stalls": 0,
        }
