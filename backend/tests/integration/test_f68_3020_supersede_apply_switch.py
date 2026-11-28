"""#3020 (f68): the Sleep supersede-apply switch as a ``neural_config`` row.

1. A fresh database migrated to head stores ``sleep_supersede_apply_enabled``
   as 'false' (category ``sleep``, type ``bool``), and
   ``NeuralMemoryConfig.from_db`` loads it off.
2. A row an operator created before f68 keeps its value (ON CONFLICT DO
   NOTHING); a rerun of the upgrade is a no-op.
3. Downgrade removes only this row; the upgrade runs again.

The revision before f68 is read from the script, so a re-pointed
``down_revision`` needs no edit here. The row-less case (a ``create_all``
database) is in tests/neural/test_config.py.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import asyncio
import os

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from config.database import to_async_database_url
from neural.config import NeuralMemoryConfig
from tests.integration.test_alembic_migrations import (
    _DEFAULT_TEST_URL,
    _downgrade_to,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f68_3020_supersede_apply_switch"
_KEY = "sleep_supersede_apply_enabled"


def _before() -> str:
    down = ScriptDirectory.from_config(_get_alembic_config()).get_revision(REVISION).down_revision
    assert isinstance(down, str)
    return down


def _row(conn):
    return conn.execute(
        text("SELECT value, value_type, category FROM neural_config WHERE key = :key"),
        {"key": _KEY},
    ).one_or_none()


def _count(conn) -> int:
    return int(conn.execute(text("SELECT count(*) FROM neural_config")).scalar_one())


def _loaded() -> bool:
    async def load() -> bool:
        url = to_async_database_url(os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_URL))
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with AsyncSession(engine) as session:
                NeuralMemoryConfig.invalidate_cache()
                config = await NeuralMemoryConfig.from_db(session)
                return config.sleep_supersede_apply_enabled
        finally:
            NeuralMemoryConfig.invalidate_cache()
            await engine.dispose()

    return asyncio.run(load())


def test_a_fresh_database_seeds_the_switch_off() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to("head")
        with engine.connect() as conn:
            row = _row(conn)
            assert row is not None
            assert (row.value, row.value_type, row.category) == ("false", "bool", "sleep")
        assert _loaded() is False
    finally:
        engine.dispose()


def test_f68_keeps_an_operators_row_and_round_trips() -> None:
    before = _before()
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(before)
        with engine.begin() as conn:
            assert _row(conn) is None
            # An operator created the row before the migration ran.
            conn.execute(
                text(
                    "INSERT INTO neural_config (key, value, value_type, category, description) "
                    "VALUES (:key, 'true', 'bool', 'sleep', 'set by an operator')"
                ),
                {"key": _KEY},
            )
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _row(conn).value == "true"
        assert _loaded() is True

        # A rerun is a no-op.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": before})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _row(conn).value == "true"
            rows_at_f68 = _count(conn)

        # Downgrade removes only this row.
        _downgrade_to(before)
        with engine.connect() as conn:
            assert _row(conn) is None
            assert _count(conn) == rows_at_f68 - 1
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _row(conn).value == "false"
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
