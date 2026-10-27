"""DB contract for the shared migration helpers (#2240).

``validate_constraint`` validates outside the ALTER's transaction with the
migration ``lock_timeout`` capping its lock wait, and resets the session
afterwards; ``index_is_invalid`` tells an interrupted ``CREATE INDEX
CONCURRENTLY`` leftover from a built index. Runs on a scratch table of the
test database, dropped afterwards.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from db.migration_helpers import (
    MIGRATION_LOCK_TIMEOUT_ENV,
    index_is_invalid,
    validate_constraint,
)
from tests.integration.test_alembic_migrations import _sync_engine

_TABLE = "t2240_migration_helpers"
_CHECK = "ck_t2240_positive"


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        conn.execute(text(f"CREATE TABLE {_TABLE} (id int PRIMARY KEY, n int NOT NULL)"))
        conn.execute(text(f"INSERT INTO {_TABLE} VALUES (1, 1), (2, 2), (3, 2)"))
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        engine.dispose()


def _run(conn: Connection, fn) -> None:
    """Run ``fn`` as a revision body: inside the migration's transaction."""
    ctx = MigrationContext.configure(connection=conn)
    with ctx.begin_transaction(), Operations.context(ctx):
        fn()


def _add_not_valid() -> None:
    from alembic import op

    op.execute(
        text(
            f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CHECK}, "
            f"ADD CONSTRAINT {_CHECK} CHECK (n > 0) NOT VALID"
        )
    )


def _validated(conn: Connection) -> bool:
    return conn.execute(
        text("SELECT convalidated FROM pg_constraint WHERE conname = :name"),
        {"name": _CHECK},
    ).scalar_one()


def test_validate_constraint_validates_after_the_add_commits(engine, monkeypatch) -> None:
    monkeypatch.setenv(MIGRATION_LOCK_TIMEOUT_ENV, "750ms")
    with engine.connect() as conn:

        def _body() -> None:
            _add_not_valid()
            validate_constraint(_TABLE, _CHECK)

        _run(conn, _body)

        assert _validated(conn) is True
        # The session-level cap is gone again after the block.
        assert conn.exec_driver_sql("SHOW lock_timeout").scalar_one() == "0"
        conn.rollback()


def test_validate_constraint_fails_fast_behind_a_blocker_and_reruns(engine, monkeypatch) -> None:
    monkeypatch.setenv(MIGRATION_LOCK_TIMEOUT_ENV, "300ms")
    with engine.connect() as conn:
        _run(conn, _add_not_valid)
        conn.commit()

        with engine.connect() as blocker:
            # SHARE UPDATE EXCLUSIVE conflicts with VALIDATE's own lock; the
            # idle timeout ends the blocker if the cap ever stops working.
            blocker.execute(text("SET idle_in_transaction_session_timeout = '20s'"))
            blocker.execute(text(f"LOCK TABLE {_TABLE} IN SHARE UPDATE EXCLUSIVE MODE"))

            started = time.monotonic()
            with pytest.raises(DBAPIError) as excinfo:
                _run(conn, lambda: validate_constraint(_TABLE, _CHECK))
            elapsed = time.monotonic() - started
            blocker.rollback()

        # psycopg2 (the sync test engine) names it pgcode.
        assert getattr(excinfo.value.orig, "pgcode", None) == "55P03"
        assert elapsed < 10
        conn.rollback()
        # The RESET ran on the way out, and the NOT VALID add stays committed.
        assert conn.exec_driver_sql("SHOW lock_timeout").scalar_one() == "0"
        assert _validated(conn) is False
        conn.rollback()

        _run(conn, lambda: validate_constraint(_TABLE, _CHECK))
        assert _validated(conn) is True
        conn.rollback()


def test_index_is_invalid_tells_a_leftover_from_a_built_index(engine) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # A unique build over duplicate values fails and leaves the index INVALID.
        with pytest.raises(DBAPIError):
            conn.execute(text(f"CREATE UNIQUE INDEX CONCURRENTLY ix_t2240_bad ON {_TABLE} (n)"))
        conn.execute(text(f"CREATE INDEX CONCURRENTLY ix_t2240_good ON {_TABLE} (n)"))

    seen: dict[str, bool] = {}
    with engine.connect() as conn:

        def _probe() -> None:
            for name in ("ix_t2240_bad", "ix_t2240_good", "ix_t2240_absent"):
                seen[name] = index_is_invalid(name)

        _run(conn, _probe)

    assert seen == {"ix_t2240_bad": True, "ix_t2240_good": False, "ix_t2240_absent": False}
