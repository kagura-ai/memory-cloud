"""#3021 (f64 and the a91 seed): Sleep phase switches on a migrated database.

1. A fresh database migrated to head stores ``sleep_edge_discovery_enabled``,
   ``sleep_importance_reeval_enabled`` and the new
   ``sleep_consolidation_judge_enabled`` as 'false', and
   ``NeuralMemoryConfig.from_db`` loads all three off.
2. A database that stored 'true' for the two older keys before f64 keeps
   them (f64 touches only its own row); a judge row an operator created
   before f64 keeps its value (ON CONFLICT DO NOTHING).
3. Downgrade removes the judge row; a rerun of the upgrade is a no-op.

The row-less case (a ``create_all`` database) is in tests/neural/test_config.py.
Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from config.database import to_async_database_url
from neural.config import NeuralMemoryConfig
from tests.integration.test_alembic_migrations import (
    _DEFAULT_TEST_URL,
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f64_3021_judge_switch"
BEFORE = "f63_3014_user_facts_lane"
_KEYS = (
    "sleep_edge_discovery_enabled",
    "sleep_importance_reeval_enabled",
    "sleep_consolidation_judge_enabled",
)


def _values(conn) -> dict[str, str]:
    rows = conn.execute(
        text("SELECT key, value FROM neural_config WHERE key = ANY(:keys)"), {"keys": list(_KEYS)}
    ).all()
    return {row.key: row.value for row in rows}


def _loaded() -> list[bool]:
    async def load() -> list[bool]:
        url = to_async_database_url(os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_URL))
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with AsyncSession(engine) as session:
                NeuralMemoryConfig.invalidate_cache()
                config = await NeuralMemoryConfig.from_db(session)
                return [getattr(config, key) for key in _KEYS]
        finally:
            NeuralMemoryConfig.invalidate_cache()
            await engine.dispose()

    return asyncio.run(load())


def test_a_fresh_database_seeds_every_switch_off() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to("head")
        with engine.connect() as conn:
            assert _values(conn) == dict.fromkeys(_KEYS, "false")
        assert _loaded() == [False, False, False]
    finally:
        engine.dispose()


def test_f64_keeps_stored_values_and_round_trips() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(BEFORE)
        with engine.begin() as conn:
            # A deployment migrated before this release stored 'true' (the old seed).
            conn.execute(
                text(
                    "UPDATE neural_config SET value = 'true' "
                    "WHERE key IN ('sleep_edge_discovery_enabled', "
                    "'sleep_importance_reeval_enabled')"
                )
            )
            assert "sleep_consolidation_judge_enabled" not in _values(conn)

        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _values(conn) == {
                "sleep_edge_discovery_enabled": "true",
                "sleep_importance_reeval_enabled": "true",
                "sleep_consolidation_judge_enabled": "false",
            }
        assert _loaded() == [True, True, False]

        # A rerun is a no-op; an operator's own judge row is kept.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE neural_config SET value = 'true' "
                    "WHERE key = 'sleep_consolidation_judge_enabled'"
                )
            )
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _values(conn)["sleep_consolidation_judge_enabled"] == "true"

        _downgrade_to(BEFORE)
        with engine.connect() as conn:
            assert "sleep_consolidation_judge_enabled" not in _values(conn)
            assert _values(conn)["sleep_edge_discovery_enabled"] == "true"
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _values(conn)["sleep_consolidation_judge_enabled"] == "false"
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
