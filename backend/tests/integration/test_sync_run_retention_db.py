"""#2916: finished runs drop their diff, keep their change count, and expire.

Real HTTP and PostgreSQL: the worker's run status and diff page, the human
diff page and the run history read the same stored count.
"""

from datetime import timedelta
from uuid import UUID

import pytest
import structlog
from sqlalchemy import delete, func, select, update

from models.sync_feed import SyncChunk, SyncIndexOutbox, SyncRun
from services import sync_feed_index
from services.sync_feed_index import RUN_RETENTION_DAYS, maintain_sync_runs
from services.sync_management import discard_pending_runs
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration import test_sync_activity_db as activity_tests
from tests.integration.sync_feed_fixtures import committed, human, product, service, staged

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds
activity = activity_tests.activity

RECORDS = [product("one"), product("two"), product("three")]
# Past the window for every run: an upload window closes 24 hours after the
# run is created, a commit happens inside it.
PAST_RETENTION = timedelta(days=RUN_RETENTION_DAYS + 1, hours=1)


async def _stored(feeds, run_id: str) -> SyncRun | None:
    async with feeds.factory() as db:
        return await db.get(SyncRun, UUID(run_id))


async def _chunks(feeds, run_id: str) -> int:
    async with feeds.factory() as db:
        return await db.scalar(
            select(func.count()).select_from(SyncChunk).where(SyncChunk.run_id == UUID(run_id))
        )


async def _age(feeds, run_id: str, by: timedelta) -> None:
    """Move every timestamp of the run back, as if it had run that long ago."""
    async with feeds.factory() as db:
        await db.execute(
            update(SyncRun)
            .where(SyncRun.id == UUID(run_id))
            .values(
                created_at=SyncRun.created_at - by,
                expires_at=SyncRun.expires_at - by,
                prepared_until=SyncRun.prepared_until - by,
                committed_at=SyncRun.committed_at - by,
            )
        )
        await db.commit()


async def _tick(feeds) -> None:
    async with feeds.factory() as db:
        await maintain_sync_runs(db)


async def _abort(feeds, run_id: str) -> None:
    result = await service(feeds, "POST", f"runs/{run_id}/abort")
    assert result.status_code == 200, result.text


async def _counts(activity, run_id: str) -> dict:
    """The run's change count in every view, and what each diff page holds."""
    status = await service(activity, "GET", f"runs/{run_id}")
    worker_diff = await service(activity, "GET", f"runs/{run_id}/diff")
    human_diff = await human(activity, "GET", f"runs/{run_id}/diff")
    detail = await human(activity, "GET", f"activity/runs/{run_id}")
    history = await human(activity, "GET", "activity/runs")
    for result in (status, worker_diff, human_diff, detail, history):
        assert result.status_code == 200, result.text
    (listed,) = [item for item in history.json()["items"] if item["run_id"] == run_id]
    return {
        "status": status.json()["changes"],
        "worker_diff": (worker_diff.json()["changes"], len(worker_diff.json()["diff"])),
        "human_diff": (human_diff.json()["changes"], len(human_diff.json()["diff"])),
        "detail": detail.json()["changes"],
        "history": listed["changes"],
    }


def _cleared(changes: int) -> dict:
    return {
        "status": changes,
        "worker_diff": (changes, 0),
        "human_diff": (changes, 0),
        "detail": changes,
        "history": changes,
    }


async def test_an_aborted_run_has_no_diff_and_keeps_its_change_count_in_every_view(activity):
    row = await staged(activity, RECORDS)
    assert row["changes"] == 3
    await _abort(activity, row["run_id"])
    stored = await _stored(activity, row["run_id"])
    assert (stored.state, stored.diff, stored.change_count) == ("aborted", None, 3)
    assert await _counts(activity, row["run_id"]) == _cleared(3)


async def test_an_expired_run_has_no_diff_and_keeps_its_change_count_in_every_view(activity):
    row = await staged(activity, RECORDS)
    await _age(activity, row["run_id"], timedelta(hours=25))
    await _tick(activity)
    stored = await _stored(activity, row["run_id"])
    assert (stored.state, stored.diff, stored.change_count) == ("expired", None, 3)
    assert await _chunks(activity, row["run_id"]) == 1
    assert await _counts(activity, row["run_id"]) == _cleared(3)


async def test_a_discarded_run_has_no_diff_and_keeps_its_change_count(activity):
    row = await staged(activity, RECORDS)
    async with activity.factory() as db:
        await discard_pending_runs(db, activity.iid)
        await db.commit()
    stored = await _stored(activity, row["run_id"])
    assert (stored.state, stored.diff, stored.change_count) == ("aborted", None, 3)
    assert await _counts(activity, row["run_id"]) == _cleared(3)


async def test_a_run_never_prepared_still_reports_no_change_count(activity):
    """Before a diff there is no count: the history keeps reporting null."""
    result = await service(
        activity, "POST", "runs", body=fixtures.manifest(activity, [product("one")])
    )
    assert result.status_code == 201, result.text
    run_id = result.json()["run_id"]
    await _abort(activity, run_id)
    stored = await _stored(activity, run_id)
    assert (stored.diff, stored.change_count) == (None, None)
    detail = await human(activity, "GET", f"activity/runs/{run_id}")
    assert detail.json()["changes"] is None
    assert (await service(activity, "GET", f"runs/{run_id}")).json()["changes"] == 0


async def test_never_committed_runs_past_the_window_are_deleted_with_their_chunks(activity):
    old_aborted = await staged(activity, RECORDS)
    await _abort(activity, old_aborted["run_id"])
    old_expired = await staged(activity, [product("four")])
    recent_aborted = await staged(activity, [product("five")])
    await _abort(activity, recent_aborted["run_id"])
    for row in (old_aborted, old_expired):
        await _age(activity, row["run_id"], PAST_RETENTION)
    await _tick(activity)  # the sweep expires the old upload
    assert (await _stored(activity, old_expired["run_id"])).state == "expired"
    await _tick(activity)
    for row in (old_aborted, old_expired):
        assert await _stored(activity, row["run_id"]) is None
        assert await _chunks(activity, row["run_id"]) == 0
        result = await service(activity, "GET", f"runs/{row['run_id']}")
        assert result.status_code == 404, result.text
    # Inside the window the run and its receipts stay and still answer.
    assert (await _stored(activity, recent_aborted["run_id"])).state == "aborted"
    assert await _chunks(activity, recent_aborted["run_id"]) == 1
    assert await _counts(activity, recent_aborted["run_id"]) == _cleared(1)


async def test_committed_runs_keep_their_rows_and_only_superseded_old_diffs_go(activity):
    """The newest commit of the feed keeps everything, however old; an older
    one keeps its row, chunk receipts and outbox receipt, and loses only its
    diff once superseded for longer than the window."""
    first = await committed(activity, [product("one")])
    second = await committed(activity, [product("one"), product("two")])
    latest = await committed(activity, [product("one"), product("two"), product("three")])
    for row in (first, latest):
        await _age(activity, row["run_id"], PAST_RETENTION)
    receipts = {row["run_id"]: await _chunks(activity, row["run_id"]) for row in (first, latest)}
    await _tick(activity)
    old = await _stored(activity, first["run_id"])
    assert (old.state, old.diff, old.change_count) == ("committed", None, 1)
    assert await _chunks(activity, first["run_id"]) == receipts[first["run_id"]]
    async with activity.factory() as db:
        outbox = await db.get(SyncIndexOutbox, UUID(first["run_id"]))
        assert outbox is not None and len(outbox.event_ids) == 1
    status = await service(activity, "GET", f"runs/{first['run_id']}")
    assert status.status_code == 200, status.text
    assert (status.json()["changes"], status.json()["index_status"]) == (1, "superseded")
    assert await _counts(activity, first["run_id"]) == _cleared(1)
    # Superseded inside the window: the diff stays.
    assert len((await _stored(activity, second["run_id"])).diff) == 1
    # The feed's newest commit, past the window: untouched.
    newest = await _stored(activity, latest["run_id"])
    assert len(newest.diff) == newest.change_count == 1
    assert await _chunks(activity, latest["run_id"]) == receipts[latest["run_id"]]
    status = await service(activity, "GET", f"runs/{latest['run_id']}")
    assert status.status_code == 200, status.text
    assert status.json()["changes"] == 1 and status.json()["index_status"] == "pending"
    worker_diff = await service(activity, "GET", f"runs/{latest['run_id']}/diff")
    assert len(worker_diff.json()["diff"]) == 1


async def test_a_failing_retention_pass_is_rolled_back_logged_and_the_tick_goes_on(
    feeds, monkeypatch
):
    old = await staged(feeds, RECORDS)
    await _abort(feeds, old["run_id"])
    await _age(feeds, old["run_id"], PAST_RETENTION)
    upload = await staged(feeds, [product("four")])
    await _age(feeds, upload["run_id"], timedelta(hours=25))

    async def failing_pass(db):
        await db.execute(delete(SyncRun).where(SyncRun.id == UUID(old["run_id"])))
        raise RuntimeError("retention failed")

    monkeypatch.setattr(sync_feed_index, "prune_finished_runs", failing_pass)
    with structlog.testing.capture_logs() as logs:
        await _tick(feeds)
    assert (await _stored(feeds, old["run_id"])).state == "aborted"
    assert (await _stored(feeds, upload["run_id"])).state == "expired"
    failed = [entry for entry in logs if entry["event"] == "sync_run_retention_failed"]
    assert [entry["error_class"] for entry in failed] == ["RuntimeError"]
    assert failed[0]["log_level"] == "error"


async def test_each_retention_pass_is_bounded(feeds, monkeypatch):
    monkeypatch.setattr(sync_feed_index, "RUN_RETENTION_BATCH", 1)
    rows = [await staged(feeds, [product(name)]) for name in ("one", "two")]
    for row in rows:
        await _abort(feeds, row["run_id"])
        await _age(feeds, row["run_id"], PAST_RETENTION)
    await _tick(feeds)
    assert [await _stored(feeds, row["run_id"]) is None for row in rows].count(True) == 1
    await _tick(feeds)
    assert [await _stored(feeds, row["run_id"]) for row in rows] == [None, None]
