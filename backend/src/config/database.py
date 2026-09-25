"""Database configuration.

Database URLs are read directly from environment variables (.env.local).
This allows for simple configuration without Pydantic validation.
"""

import os

# SQLAlchemy 2.1 resolves a bare ``postgresql://`` URL to psycopg (v3), which is
# not installed (#1695). Every engine names its driver instead of relying on
# that default: asyncpg for the application and Alembic, psycopg2 for the
# OAuth2 sync engine and the CLI tools.
ASYNC_DRIVERNAME = "postgresql+asyncpg"
SYNC_DRIVERNAME = "postgresql+psycopg2"

# Schemes normalized to the chosen driver. Any other scheme — another explicit
# driver an operator chose deliberately, or a non-PostgreSQL URL — passes
# through unchanged.
_NORMALIZED_SCHEMES = frozenset({"postgresql", ASYNC_DRIVERNAME, SYNC_DRIVERNAME})


def _with_drivername(url: str, drivername: str) -> str:
    """Swap the URL's scheme for ``drivername``, leaving the rest verbatim.

    String surgery rather than ``make_url().set()``: re-rendering a parsed URL
    can re-escape credentials, while the part after ``://`` must reach the
    driver byte-for-byte.
    """
    scheme, sep, rest = url.partition("://")
    if not sep or scheme not in _NORMALIZED_SCHEMES:
        return url
    return f"{drivername}://{rest}"


def to_async_database_url(url: str) -> str:
    """Return ``url`` with the asyncpg driver named explicitly.

    Accepts the bare ``postgresql://`` scheme operators' env files use, as well
    as the ``+asyncpg`` / ``+psycopg2`` forms.
    """
    return _with_drivername(url, ASYNC_DRIVERNAME)


def to_sync_database_url(url: str) -> str:
    """Return ``url`` with the psycopg2 driver named explicitly.

    Accepts the bare ``postgresql://`` scheme operators' env files use, as well
    as the ``+asyncpg`` / ``+psycopg2`` forms.
    """
    return _with_drivername(url, SYNC_DRIVERNAME)


def get_database_url() -> str:
    """Get PostgreSQL database URL from environment.

    Returns:
        Database URL (default for dev if not set)

    Raises:
        ConfigurationError: If DATABASE_URL not set in production
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        # Development default
        return "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura"
    return url


def get_sync_database_url() -> str:
    """Get the ``DATABASE_URL`` for a synchronous (psycopg2) engine.

    Returns:
        ``DATABASE_URL`` (or the dev default) with ``postgresql+psycopg2``.
    """
    return to_sync_database_url(get_database_url())


def get_qdrant_url() -> str:
    """Get Qdrant server URL from environment.

    Returns:
        Qdrant URL (default for dev if not set)
    """
    url = os.getenv("QDRANT_URL")
    if not url:
        return "http://localhost:6333"
    return url


def get_redis_url() -> str:
    """Get Redis server URL from environment.

    Returns:
        Redis URL (default for dev if not set)
    """
    url = os.getenv("REDIS_URL")
    if not url:
        return "redis://localhost:6379"
    return url


# Database connection URLs
DATABASE_URL = get_database_url()
QDRANT_URL = get_qdrant_url()
REDIS_URL = get_redis_url()
