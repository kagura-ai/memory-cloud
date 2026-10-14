"""``db.base`` engines read their pool sizing from Settings (#1978).

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
    for var in _POOL_ENV:
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch
    if db_base.sync_engine is not None:
        db_base.sync_engine.dispose()
    if db_base.engine is not None:
        db_base.engine.sync_engine.dispose()


def _pool_values(engine) -> tuple[int, int, float]:
    pool = engine.pool
    return pool.size(), pool._max_overflow, pool._timeout


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
    """0 / -1 mean "unlimited" to SQLAlchemy; Settings refuses them."""
    from pydantic import ValidationError

    fresh_engines.setenv(var, value)
    with pytest.raises(ValidationError):
        settings_module.Settings()
