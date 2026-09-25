"""``db.base`` engines name their PostgreSQL driver explicitly (#1695).

The OAuth2 sync engine used to derive its URL by stripping ``+asyncpg``, which
on SQLAlchemy 2.1 selects psycopg (v3) — not installed — and broke every
OAuth2 flow at first use. Neither engine may depend on SQLAlchemy's default
PostgreSQL driver, whichever scheme ``DATABASE_URL`` arrives with.

The engines are built for real (no connection is opened): dialect resolution
and the DBAPI import are what failed, so a mock would hide the regression.
"""

from __future__ import annotations

import pytest

import config.database as config_database
import db.base as db_base

_REST = "kagura:s3cret@db.example:5432/kagura"

_SCHEMES = ["postgresql", "postgresql+asyncpg", "postgresql+psycopg2"]


@pytest.fixture
def fresh_engines(monkeypatch: pytest.MonkeyPatch):
    """Clear the lazy singletons so the getters build new engines; restore after."""
    for name in ("engine", "sync_engine", "async_session_factory", "sync_session_factory"):
        monkeypatch.setattr(db_base, name, None)
    return monkeypatch


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_sync_engine_uses_psycopg2(scheme: str, fresh_engines: pytest.MonkeyPatch) -> None:
    fresh_engines.setattr(config_database, "DATABASE_URL", f"{scheme}://{_REST}")
    engine = db_base._get_sync_engine()
    try:
        assert engine.dialect.driver == "psycopg2"
        assert engine.url.drivername == "postgresql+psycopg2"
    finally:
        engine.dispose()


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_async_engine_uses_asyncpg(scheme: str, fresh_engines: pytest.MonkeyPatch) -> None:
    fresh_engines.setattr(config_database, "DATABASE_URL", f"{scheme}://{_REST}")
    engine = db_base._get_engine()
    assert engine.dialect.driver == "asyncpg"
    assert engine.url.drivername == "postgresql+asyncpg"
