"""DB contract for the migration lock_timeout (#2300).

``alembic/env.py`` starts every migration transaction with ``SET LOCAL
lock_timeout`` (``db.migration_helpers``). An ALTER that cannot get its ACCESS
EXCLUSIVE lock within it fails with SQLSTATE 55P03 (``lock_not_available``)
and the revision rolls back, instead of queueing behind the blocker and
stalling every later query on the table.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest
from alembic.migration import MigrationContext
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from config.database import to_async_database_url
from db.migration_helpers import (
    MIGRATION_LOCK_TIMEOUT_ENV,
    install_migration_lock_timeout,
    reapply_migration_lock_timeout,
)
from tests.integration.test_alembic_migrations import (
    _DEFAULT_TEST_URL,
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_and_upgrade_to,
    _sync_engine,
)

PRE_F13_REV = "f12_2244_rev_writer_erasure"
F13_REV = "f13_1952_api_keys_connector_id"


def _show(conn: Connection) -> str:
    return conn.exec_driver_sql("SHOW lock_timeout").scalar_one()


def _run_on_migration_connection(fn, value: str | None) -> list[tuple[str, str]]:
    """Run ``fn(ctx, seen)`` on an async-engine connection, the env.py shape."""
    import asyncio

    url = to_async_database_url(os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_URL))
    seen: list[tuple[str, str]] = []

    def _sync(conn: Connection) -> None:
        install_migration_lock_timeout(conn, value)
        ctx = MigrationContext.configure(connection=conn)
        fn(ctx, seen)

    async def _main() -> None:
        engine = create_async_engine(url, poolclass=pool.NullPool)
        try:
            async with engine.connect() as conn:
                await conn.run_sync(_sync)
        finally:
            await engine.dispose()

    asyncio.run(_main())
    return seen


def _walk(ctx: MigrationContext, seen: list[tuple[str, str]]) -> None:
    with ctx.begin_transaction():
        seen.append(("first transaction", _show(ctx.connection)))
        # A revision's own RESET is undone for the next revision.
        ctx.connection.exec_driver_sql("RESET lock_timeout")
        reapply_migration_lock_timeout("750ms")(ctx=ctx, step=None, heads=set(), run_args={})
        seen.append(("next revision", _show(ctx.connection)))
        with ctx.autocommit_block():
            seen.append(("autocommit block", _show(ctx.connection)))
        seen.append(("after the block", _show(ctx.connection)))


def test_every_migration_transaction_gets_the_lock_timeout() -> None:
    seen = _run_on_migration_connection(_walk, "750ms")

    assert seen == [
        ("first transaction", "750ms"),
        ("next revision", "750ms"),
        # CONCURRENTLY and the other autocommit statements keep no cap.
        ("autocommit block", "0"),
        ("after the block", "750ms"),
    ]


def test_turned_off_sends_no_set() -> None:
    def _first(ctx: MigrationContext, seen: list[tuple[str, str]]) -> None:
        with ctx.begin_transaction():
            seen.append(("first transaction", _show(ctx.connection)))

    assert _run_on_migration_connection(_first, None) == [("first transaction", "0")]


@pytest.fixture
def short_lock_timeout() -> Iterator[None]:
    prev = os.environ.get(MIGRATION_LOCK_TIMEOUT_ENV)
    os.environ[MIGRATION_LOCK_TIMEOUT_ENV] = "300ms"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(MIGRATION_LOCK_TIMEOUT_ENV, None)
        else:
            os.environ[MIGRATION_LOCK_TIMEOUT_ENV] = prev


def _current_revision(conn: Connection) -> str:
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _has_connector_id(conn: Connection) -> bool:
    return (
        conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'api_keys' AND column_name = 'connector_id'"
            )
        ).scalar_one()
        == 1
    )


class TestAlterFailsFastBehindABlocker:
    def test_f13_alter_times_out_and_reruns_cleanly(self, short_lock_timeout: None) -> None:
        try:
            _reset_and_upgrade_to(PRE_F13_REV)
            engine = _sync_engine()
            try:
                with engine.connect() as blocker:
                    # Reading api_keys in an open transaction holds ACCESS
                    # SHARE until it ends, which conflicts with the ALTER's
                    # ACCESS EXCLUSIVE. The idle timeout ends the blocker if
                    # the cap ever stops working, so a regression fails the
                    # assertion below instead of hanging the run.
                    blocker.execute(text("SET idle_in_transaction_session_timeout = '20s'"))
                    blocker.execute(text("SELECT count(*) FROM api_keys")).scalar_one()

                    started = time.monotonic()
                    with pytest.raises(DBAPIError) as excinfo:
                        with _alembic_at_test_db():
                            command.upgrade(_get_alembic_config(), F13_REV)
                    elapsed = time.monotonic() - started
                    blocker.rollback()

                assert "lock timeout" in str(excinfo.value)
                assert getattr(excinfo.value.orig, "sqlstate", None) == "55P03"
                assert elapsed < 10

                # Nothing of f13 was kept: the rerun starts from f12.
                with engine.connect() as conn:
                    assert _current_revision(conn) == PRE_F13_REV
                    assert not _has_connector_id(conn)

                with _alembic_at_test_db():
                    command.upgrade(_get_alembic_config(), F13_REV)
                with engine.connect() as conn:
                    assert _current_revision(conn) == F13_REV
                    assert _has_connector_id(conn)
            finally:
                engine.dispose()
        finally:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")
