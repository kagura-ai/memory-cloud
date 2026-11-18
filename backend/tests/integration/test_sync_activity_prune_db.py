"""#2915: the sync activity retention is indexed, hourly and isolated from the tick."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import delete, event, select, text

from config.settings import get_settings
from models.auth import AuditLog
from models.sync_feed import SyncRun
from services import sync_audit, sync_feed_index
from services.sync_feed_index import ActivityPruneSchedule, maintain_sync_runs
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_feed_fixtures import product, staged
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


@pytest.fixture
def fresh_schedule(monkeypatch):
    """A process that has not pruned yet; ``clock`` drives its monotonic time."""
    clock = [10_000.0]
    monkeypatch.setattr(sync_feed_index, "activity_prune_schedule", ActivityPruneSchedule())
    monkeypatch.setattr(sync_feed_index, "monotonic", lambda: clock[0])
    return clock


def _activity(*, age: timedelta, email: str = sync_audit.ACTIVITY_EMAIL, schema=None) -> AuditLog:
    return AuditLog(
        user_id="sync-subject-" + uuid4().hex,
        user_email=email,
        action="sync.run.prepared",
        resource="sync:" + str(uuid4()),
        user_metadata={"schema": sync_audit.SCHEMA if schema is None else schema},
        created_at=utcnow() - age,
    )


async def test_every_sync_activity_row_is_written_with_the_indexed_user_email():
    """``add_audit`` is the only writer of the activity schema (no migration backfills it)."""
    from services.sync_audit import AuditActor, add_audit

    class _Collect:
        def __init__(self) -> None:
            self.rows: list[AuditLog] = []

        def add(self, row: AuditLog) -> None:
            self.rows.append(row)

    db = _Collect()
    add_audit(db, "sync.run.prepared", AuditActor(kind="system", subject="system"))  # type: ignore[arg-type]
    assert [row.user_email for row in db.rows] == [sync_audit.ACTIVITY_EMAIL]
    assert [row.user_metadata["schema"] for row in db.rows] == [sync_audit.SCHEMA]
    assert sync_audit.ACTIVITY_EMAIL == "system:sync-audit"


async def test_prune_selector_uses_the_user_email_index_not_a_scan_of_audit_logs(feeds):
    """Enough aged rows of other producers that a scan would be the planner's loss."""
    marker = "explain-" + uuid4().hex
    async with feeds.factory() as db:
        await db.execute(
            text(
                "INSERT INTO audit_logs (user_email, user_id, action, resource, user_metadata, "
                "created_at) SELECT 'producer-' || (g % 50) || '@example.com', 'user-' || g, "
                "'config_update', :marker || '-' || g, CAST(:meta AS json), :old "
                "FROM generate_series(1, 20000) AS g"
            ),
            {
                "marker": marker,
                "meta": '{"note": "another producer"}',
                "old": utcnow() - timedelta(days=200),
            },
        )
        db.add_all([_activity(age=timedelta(days=120)) for _ in range(20)])
        db.add_all([_activity(age=timedelta(days=1)) for _ in range(20)])
        await db.commit()
        await db.execute(text("ANALYZE audit_logs"))
        await db.commit()
        cutoff = utcnow() - timedelta(days=sync_audit.RETENTION_DAYS)
        selector = sync_audit.expired_activity_ids(cutoff, 500)
        sql = str(selector.compile(dialect=db.bind.dialect, compile_kwargs={"literal_binds": True}))
        connection = await db.connection()
        plan = "\n".join(
            row[0] for row in (await connection.exec_driver_sql("EXPLAIN " + sql)).all()
        )
        await db.execute(delete(AuditLog).where(AuditLog.resource.like(marker + "-%")))
        await db.commit()
    assert "Seq Scan on audit_logs" not in plan, plan
    assert "ix_audit_logs_user_email" in plan, plan


async def test_prune_deletes_expired_sync_activity_and_leaves_other_producers(feeds):
    expired = _activity(age=timedelta(days=91))
    recent = _activity(age=timedelta(days=89))
    other_schema = _activity(age=timedelta(days=91), schema="other-schema-v1")
    other_producer = _activity(age=timedelta(days=91), email="system:legacy")
    async with feeds.factory() as db:
        db.add_all([expired, recent, other_schema, other_producer])
        await db.commit()
        ids = {
            "expired": expired.id,
            "recent": recent.id,
            "other_schema": other_schema.id,
            "other_producer": other_producer.id,
        }
        await sync_audit.prune_activity(db)
        await db.commit()
        left = set(
            (await db.scalars(select(AuditLog.id).where(AuditLog.id.in_(list(ids.values()))))).all()
        )
    assert left == {ids["recent"], ids["other_schema"], ids["other_producer"]}


async def test_failing_prune_is_rolled_back_logged_and_the_run_sweep_still_commits(
    feeds, monkeypatch, fresh_schedule
):
    row = await staged(feeds, [product()])
    sentinel = _activity(age=timedelta(days=91))
    async with feeds.factory() as db:
        db.add(sentinel)
        await db.commit()
        sentinel_id = sentinel.id
    feeds.store.clock.now += timedelta(hours=25)  # the staged run is past its expiry

    async def failing_prune(db, **_kwargs):
        await db.execute(delete(AuditLog).where(AuditLog.id == sentinel_id))
        raise RuntimeError("prune failed")

    monkeypatch.setattr(sync_audit, "prune_activity", failing_prune)
    with structlog.testing.capture_logs() as logs:
        async with feeds.factory() as db:
            await maintain_sync_runs(db)
    async with feeds.factory() as db:
        assert (await db.get(SyncRun, UUID(row["run_id"]))).state == "expired"
        assert await db.get(AuditLog, sentinel_id) is not None
    failed = [entry for entry in logs if entry["event"] == "sync_activity_prune_failed"]
    assert [entry["error_class"] for entry in failed] == ["RuntimeError"]
    assert failed[0]["log_level"] == "error"


async def test_with_every_sync_switch_off_a_tick_makes_one_audit_lookup_per_interval(
    feeds, monkeypatch, fresh_schedule
):
    settings = get_settings()
    for name in (
        "oidc_enabled",
        "sync_integrations_enabled",
        "sync_runs_enabled",
        "sync_managed_enabled",
        "sync_observability_enabled",
    ):
        assert hasattr(settings, name), name
        monkeypatch.setattr(settings, name, False)
    lookups: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        if "FROM audit_logs" in statement:
            lookups.append(statement)

    async with feeds.factory() as db:
        engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        for _tick in range(12):  # an hour of five-minute ticks, the first after start
            async with feeds.factory() as db:
                await maintain_sync_runs(db)
            fresh_schedule[0] += 299
        assert len(lookups) == 1
        fresh_schedule[0] += 300
        async with feeds.factory() as db:
            await maintain_sync_runs(db)
        assert len(lookups) == 2
    finally:
        event.remove(engine, "before_cursor_execute", record)
