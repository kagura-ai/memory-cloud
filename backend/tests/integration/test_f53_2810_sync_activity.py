"""New attribution metadata round-trips without altering old canonical/run receipts."""

from sqlalchemy import inspect, text

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)
from tests.integration.test_f50_2807_merge_sync_heads import (
    _assert_preserved,
    _seed_common,
    _seed_sync,
)


def _run_columns(engine):
    return [
        (c["name"], str(c["type"]), c["nullable"], c["default"])
        for c in inspect(engine).get_columns("sync_runs")
    ]


def test_activity_migration_preserves_existing_runs_and_round_trips():
    engine = _sync_engine()
    try:
        _reset_alembic_state()
        _upgrade_to("f52_2808_merge_feed_domains")
        ids = _seed_common(engine)
        _seed_sync(engine, ids)
        before = _run_columns(engine)
        _upgrade_to("f53_2810_sync_activity")
        assert _run_columns(engine) == before
        assert {"sync_manual_requests", "sync_manual_runs"}.issubset(
            inspect(engine).get_table_names()
        )
        _assert_preserved(engine, ids, sync=True, budget=False)
        with engine.connect() as conn:
            assert (
                conn.scalar(text("SELECT version_num FROM alembic_version"))
                == "f53_2810_sync_activity"
            )
        _downgrade_to("f52_2808_merge_feed_domains")
        assert not {"sync_manual_requests", "sync_manual_runs"}.intersection(
            inspect(engine).get_table_names()
        )
        _assert_preserved(engine, ids, sync=True, budget=False)
        _upgrade_to("head")
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
