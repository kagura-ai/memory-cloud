"""#2905: which failure records make a run's ``index_status`` read ``failed``.

The status as a worker reads it, after real batches, is in
``tests/integration/test_sync_feed_index_db.py``.
"""

from services.resource_indexer import MAX_RECORDED_FAILED_EVENTS
from services.sync_feed_index import run_failure_recorded


def _record(*event_ids: int) -> dict:
    return {
        "failed_events": [
            {"event_id": event_id, "doc_id": f"doc-{event_id}", "error_class": "RuntimeError"}
            for event_id in event_ids
        ]
    }


def test_a_recorded_event_of_the_run_is_a_failure() -> None:
    assert run_failure_recorded(_record(7, 42), [40, 41, 42])


def test_failures_of_other_runs_and_no_record_are_not() -> None:
    assert not run_failure_recorded(_record(7, 12), [40, 41, 42])
    assert not run_failure_recorded(_record(), [40, 41, 42])
    assert not run_failure_recorded({}, [40, 41, 42])
    assert not run_failure_recorded(None, [40])
    assert not run_failure_recorded(_record(40), [])


def test_a_full_record_below_the_runs_last_event_may_have_dropped_its_failure() -> None:
    """The record keeps the lowest ids: once full, a later failure is not in it."""
    even = range(2, 2 * MAX_RECORDED_FAILED_EVENTS + 1, 2)
    full = _record(*even)
    assert run_failure_recorded(full, [even[-1] + 1])
    assert run_failure_recorded(full, [even[-1] - 1, even[-1] + 1])
    # A failure of a run inside the full record's range would be in it.
    assert not run_failure_recorded(full, [even[-1] - 1])
    # Below the cap nothing was dropped.
    assert not run_failure_recorded(_record(*even[:-1]), [even[-1] + 1])


def test_hand_edited_values_read_as_no_record() -> None:
    """The runbook edits the record by hand; a malformed entry is not a failure."""
    assert not run_failure_recorded({"failed_events": "cleared"}, [3])
    assert not run_failure_recorded({"failed_events": [{"event_id": "3"}]}, [3])
    assert not run_failure_recorded(_record(3), [])
