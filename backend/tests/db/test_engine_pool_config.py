"""``db.base`` engines read their pool sizing from Settings (#1980).

A production bench exhausted the hardcoded 5 + 10 pool at concurrency 8, so
``DB_POOL_SIZE`` / ``DB_MAX_OVERFLOW`` / ``DB_POOL_TIMEOUT`` size both the
async engine and the OAuth2 sync engine. The defaults stay 5 / 10 / 30.

The engines are built for real; ``create_engine`` does not connect, so the
pool attributes can be read offline.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import config.database as config_database
import config.settings as settings_module
import db.base as db_base

_POOL_ENV = ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT")


@pytest.fixture
def fresh_engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Clear the engine singletons and the settings cache; restore after."""
    for name in ("engine", "sync_engine", "async_session_factory", "sync_session_factory"):
        monkeypatch.setattr(db_base, name, None)
    monkeypatch.setattr(config_database, "DATABASE_URL", "postgresql://u:p@localhost:5432/x")
    monkeypatch.setattr(settings_module, "_settings", None)
    # ``Settings.model_config`` captured ``ENV_FILE`` (default .env.dev) when
    # the module was imported, so switch the dotenv source off on the config
    # itself; a developer's local .env.dev must not change the defaults.
    monkeypatch.setitem(settings_module.Settings.model_config, "env_file", None)
    for var in _POOL_ENV:
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch
    if db_base.sync_engine is not None:
        db_base.sync_engine.dispose()
    if db_base.engine is not None:
        db_base.engine.sync_engine.dispose()


def _pool_values(engine) -> tuple[int, int, float]:
    pool = engine.pool
    # ``size()`` and ``timeout()`` are public; QueuePool has no accessor for
    # the overflow limit.
    return pool.size(), pool._max_overflow, pool.timeout()


def test_defaults_are_unchanged(fresh_engines: pytest.MonkeyPatch) -> None:
    assert _pool_values(db_base._get_engine()) == (5, 10, 30.0)
    assert _pool_values(db_base._get_sync_engine()) == (5, 10, 30.0)


def test_env_vars_size_both_engines(fresh_engines: pytest.MonkeyPatch) -> None:
    fresh_engines.setenv("DB_POOL_SIZE", "7")
    fresh_engines.setenv("DB_MAX_OVERFLOW", "3")
    fresh_engines.setenv("DB_POOL_TIMEOUT", "4.5")

    assert _pool_values(db_base._get_engine()) == (7, 3, 4.5)
    assert _pool_values(db_base._get_sync_engine()) == (7, 3, 4.5)


@pytest.mark.parametrize(
    ("var", "value"),
    [("DB_POOL_SIZE", "0"), ("DB_MAX_OVERFLOW", "-1"), ("DB_POOL_TIMEOUT", "0")],
)
def test_unlimited_values_are_rejected(
    fresh_engines: pytest.MonkeyPatch, var: str, value: str
) -> None:
    """pool_size 0 / max_overflow -1 mean "unlimited" to SQLAlchemy, and a zero
    timeout makes the async checkout cancel before it can take an idle
    connection; Settings refuses all three."""
    from pydantic import ValidationError

    fresh_engines.setenv(var, value)
    with pytest.raises(ValidationError):
        settings_module.Settings()


@pytest.mark.parametrize(
    ("pool_size", "max_overflow", "warns"),
    [(5, 10, False), (5, 9, True), (15, 0, False), (1, 0, True)],
)
def test_fanout_floor_warning(
    monkeypatch: pytest.MonkeyPatch, pool_size: int, max_overflow: int, warns: bool
) -> None:
    """``database_pool_below_fanout`` fires only when size + overflow < 15 (#1980)."""
    from unittest.mock import MagicMock

    log = MagicMock()
    monkeypatch.setattr(db_base, "logger", log)

    db_base._warn_if_pool_below_fanout(pool_size, max_overflow)

    if warns:
        log.warning.assert_called_once()
        assert log.warning.call_args.args == ("database_pool_below_fanout",)
        assert log.warning.call_args.kwargs["total"] == pool_size + max_overflow
    else:
        log.warning.assert_not_called()
