"""URL redaction utilities for safe logging.

Provides helpers that strip passwords from database and service URLs before
they are emitted to logs. Use these for every ``logger.info("...", url=...)``
call site so credentials never leak via stdout, ``docker logs``, or log
aggregators.

Issue #272: ``db/base.py`` logger exposed the Postgres password; ``db/redis.py``
had the same class of bug for the Redis URL.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_REDACTED = "<redacted-url>"


def redact_db_url(url: str) -> str:
    """Return a log-safe SQLAlchemy URL with the password removed.

    Uses SQLAlchemy's canonical ``make_url(...).render_as_string(hide_password=True)``
    which correctly handles dialect schemes like ``postgresql+asyncpg``, URLs
    with no password, and URLs with percent-encoded characters in the password.

    Args:
        url: A SQLAlchemy connection URL (e.g. the value of ``DATABASE_URL``).

    Returns:
        The URL with the password replaced by ``***`` if one was present,
        or a safe placeholder if the input cannot be parsed.

    Example:
        >>> redact_db_url("postgresql+asyncpg://kagura:s3cret@db:5432/app")
        'postgresql+asyncpg://kagura:***@db:5432/app'
        >>> redact_db_url("postgresql://kagura@db/app")
        'postgresql://kagura@db/app'
    """
    if not url:
        return _REDACTED
    try:
        return make_url(url).render_as_string(hide_password=True)
    except (ArgumentError, ValueError, TypeError):
        return _REDACTED


def redact_generic_url(url: str) -> str:
    """Return a log-safe generic URL (Redis, Qdrant, HTTP) with password removed.

    Parses the URL with :func:`urllib.parse.urlparse` and rewrites ``netloc``
    to drop the password while preserving scheme, user, host, port, path, and
    query. Use this for non-SQLAlchemy URLs — for SQLAlchemy URLs use
    :func:`redact_db_url` instead.

    Args:
        url: A generic URL string (e.g. ``redis://:pass@host:6379/0``).

    Returns:
        The URL with the password replaced by ``***``, or a safe placeholder
        if the input cannot be parsed.

    Example:
        >>> redact_generic_url("redis://:s3cret@redis:6379/0")
        'redis://:***@redis:6379/0'
        >>> redact_generic_url("redis://redis:6379/0")
        'redis://redis:6379/0'
    """
    if not url:
        return _REDACTED
    try:
        parsed = urlparse(url)
        if "@" not in parsed.netloc:
            # Either no credentials present (e.g. "redis://host:6379"), OR
            # urlparse did not recognize the string as a URL and the `@` was
            # pushed into path (e.g. scheme-less "user:pw@host" parses as
            # scheme="user", netloc="", path="pw@host"). If the raw input
            # contains `@`, we cannot safely extract credentials — return the
            # placeholder rather than risk logging the password.
            if "@" in url:
                return _REDACTED
            return url
        userinfo, _, host = parsed.netloc.rpartition("@")
        if ":" in userinfo:
            user, _, _ = userinfo.partition(":")
            safe_userinfo = f"{user}:***"
        else:
            # User but no password — preserve as-is, do not fabricate a marker
            safe_userinfo = userinfo
        safe_netloc = f"{safe_userinfo}@{host}" if safe_userinfo else host
        return urlunparse(parsed._replace(netloc=safe_netloc))
    except (ValueError, TypeError):
        return _REDACTED
