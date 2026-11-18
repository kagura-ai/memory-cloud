"""#2916: a run's change count follows its diff and outlives a cleared one.

The database side (abort, expiry, discard, retention and the history query)
is in ``tests/integration/test_sync_run_retention_db.py``.
"""

from datetime import timedelta
from uuid import uuid4

from models.sync_feed import SyncRun
from services.sync_feed_runs import change_count, clear_diff, run_view
from utils.datetime import utcnow


def _run(**fields) -> SyncRun:
    return SyncRun(
        **{
            "id": uuid4(),
            "feed_id": uuid4(),
            "operation_id": uuid4(),
            "request_hash": "2" * 64,
            "manifest": {},
            "generations": [1, 1, 1],
            "base_revision": 0,
            "state": "prepared",
            "expires_at": utcnow() + timedelta(hours=24),
            **fields,
        }
    )


def _diff(size: int) -> list[dict]:
    return [{"external_id": str(n), "op": "upsert"} for n in range(size)]


def test_every_stored_diff_sets_the_count() -> None:
    row = _run(diff=_diff(3))
    assert row.change_count == 3
    row.diff = _diff(5)  # a fresh prepare, or the resume commit
    assert row.change_count == 5
    row.diff = []
    assert row.change_count == 0


def test_clearing_the_diff_keeps_the_count_in_the_run_view() -> None:
    row = _run(diff=_diff(4))
    clear_diff(row)
    assert (row.diff, row.change_count) == (None, 4)
    assert run_view(row)["changes"] == 4


def test_a_diff_stored_without_a_count_is_counted_when_cleared() -> None:
    """A row an older image prepared during the deploy: no count yet."""
    row = _run()
    row.__dict__["diff"] = _diff(2)  # loaded as stored, no attribute event
    assert row.change_count is None
    assert change_count(row) == 2
    assert run_view(row)["changes"] == 2
    clear_diff(row)
    assert (row.diff, row.change_count) == (None, 2)


def test_a_run_without_a_diff_has_no_count() -> None:
    row = _run(state="uploading")
    assert change_count(row) is None
    clear_diff(row)
    assert (row.diff, row.change_count) == (None, None)
    assert run_view(row)["changes"] == 0
