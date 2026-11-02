"""Real Alembic upgrade/retry/downgrade for the deferred point delete queue (#2476)."""

from uuid import uuid4

from sqlalchemy import text

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _downgrade_to,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

PRIOR = "f20_2399_oauth_token_storage"
REVISION = "f21_2476_deferred_point_deletes"


def test_f21_creates_the_queue_without_a_memories_fk_and_downgrades_cleanly():
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(PRIOR)
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            columns = dict(
                conn.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name='deferred_point_deletes'"
                    )
                ).all()
            )
            assert columns == {
                "id": "uuid",
                "memory_id": "uuid",
                "point_id": "uuid",
                "collection": "character varying",
                "queued_at": "timestamp with time zone",
                "attempts": "integer",
            }
            # The retry must outlive the purge and erasure's hard delete.
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint "
                        "WHERE conrelid='deferred_point_deletes'::regclass AND contype='f'"
                    )
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT to_regclass('public.ix_deferred_point_deletes_queued')")
                ).scalar()
                is not None
            )
        memory_id, point_id = uuid4(), uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO deferred_point_deletes (id, memory_id, point_id, collection) "
                    "VALUES (:id, :m, :p, 'kagura_memories')"
                ),
                {"id": uuid4(), "m": memory_id, "p": point_id},
            )
            row = conn.execute(
                text("SELECT attempts, queued_at FROM deferred_point_deletes WHERE memory_id=:m"),
                {"m": memory_id},
            ).one()
            assert row.attempts == 0
            assert row.queued_at is not None
        # A retry after a lost revision stamp is a no-op and keeps the rows.
        with _alembic_at_test_db():
            command.stamp(_get_alembic_config(), PRIOR)
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM deferred_point_deletes")).scalar_one() == 1
            )
        _downgrade_to(PRIOR)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('public.deferred_point_deletes')")).scalar()
                is None
            )
    finally:
        engine.dispose()
        _reset_alembic_state()
        _upgrade_to("head")
