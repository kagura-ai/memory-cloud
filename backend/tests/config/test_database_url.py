"""PostgreSQL URL normalization for the async and sync engines (#1695).

SQLAlchemy 2.1 resolves a bare ``postgresql://`` URL to psycopg (v3), which
the backend does not install. Every engine therefore names its driver: the
application and Alembic use ``postgresql+asyncpg``, the OAuth2 sync engine and
the CLI tools use ``postgresql+psycopg2``. Operators' env files (and
``.env.example``) still carry the bare scheme, so it has to be accepted and
normalized rather than passed through to SQLAlchemy's default.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from config.database import (
    get_sync_database_url,
    to_async_database_url,
    to_sync_database_url,
)

_REST = "kagura:s3cret@db.example:5432/kagura"


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://{_REST}",
        f"postgresql+asyncpg://{_REST}",
        f"postgresql+psycopg2://{_REST}",
    ],
)
def test_sync_url_names_psycopg2(url: str) -> None:
    assert to_sync_database_url(url) == f"postgresql+psycopg2://{_REST}"


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://{_REST}",
        f"postgresql+asyncpg://{_REST}",
        f"postgresql+psycopg2://{_REST}",
    ],
)
def test_async_url_names_asyncpg(url: str) -> None:
    assert to_async_database_url(url) == f"postgresql+asyncpg://{_REST}"


def test_only_the_scheme_is_rewritten() -> None:
    """Percent-encoded credentials and the query string survive verbatim."""
    url = "postgresql+asyncpg://kagura:p%40ss%2Fword@db:5432/kagura?sslmode=require"
    assert to_sync_database_url(url) == (
        "postgresql+psycopg2://kagura:p%40ss%2Fword@db:5432/kagura?sslmode=require"
    )


@pytest.mark.parametrize(
    "url",
    [
        # An operator who names another driver chose it deliberately.
        f"postgresql+psycopg://{_REST}",
        # Not a SQLAlchemy dialect name — unsupported before #1695 and after;
        # SQLAlchemy's own error stays the signal.
        f"postgres://{_REST}",
        "sqlite://",
    ],
)
def test_other_schemes_pass_through(url: str) -> None:
    assert to_sync_database_url(url) == url
    assert to_async_database_url(url) == url


def test_bare_url_does_not_reach_the_default_driver() -> None:
    """The normalized URLs load the installed drivers, not SQLAlchemy's default.

    ``create_engine`` resolves the dialect and imports the DBAPI without
    connecting, so this is the exact step that raised
    ``ModuleNotFoundError: No module named 'psycopg'`` on 2.1.
    """
    bare = f"postgresql://{_REST}"

    sync_engine = create_engine(to_sync_database_url(bare))
    try:
        assert sync_engine.dialect.driver == "psycopg2"
    finally:
        sync_engine.dispose()

    async_engine = create_async_engine(to_async_database_url(bare))
    assert async_engine.dialect.driver == "asyncpg"


@pytest.mark.parametrize(
    "env_value",
    [
        f"postgresql://{_REST}",
        f"postgresql+asyncpg://{_REST}",
    ],
)
def test_get_sync_database_url_reads_env(env_value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", env_value)
    assert get_sync_database_url() == f"postgresql+psycopg2://{_REST}"
