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

    Behavior on various inputs:

    - Well-formed URL with credentials in netloc → credentials redacted,
      rest of URL preserved.
    - Well-formed URL (scheme and netloc both present) with no credentials
      in netloc → returned unchanged, even if ``@`` appears elsewhere
      (e.g. in path or query — such ``@`` is not a credential).
    - Malformed / scheme-less / unrecognizable input → returns the
      placeholder ``<redacted-url>``. This is fail-closed: if the string
      is not a URL we can reason about, we refuse to log it verbatim
      rather than risk a credential (or raw token) leaking through.

    Args:
        url: A generic URL string (e.g. ``redis://:pass@host:6379/0``).

    Returns:
        The URL with the password replaced by ``***``, or the placeholder
        ``<redacted-url>`` if the input is not a well-formed URL.

    Example:
        >>> redact_generic_url("redis://:s3cret@redis:6379/0")
        'redis://:***@redis:6379/0'
        >>> redact_generic_url("redis://redis:6379/0")
        'redis://redis:6379/0'
        >>> redact_generic_url("not a url")
        '<redacted-url>'
    """
    if not url:
        return _REDACTED
    try:
        parsed = urlparse(url)

        # Credentials in the netloc (the @-before-host form) — redact them,
        # but ONLY if urlparse recognized a scheme. Scheme-less network-path
        # references like "//user:pw@host" do populate netloc, but they are
        # not something a log-site caller should be passing to this helper.
        # Treat them as malformed input and fall through to the fail-closed
        # branch below, matching the docstring contract.
        if "@" in parsed.netloc and parsed.scheme:
            userinfo, _, host = parsed.netloc.rpartition("@")
            if ":" in userinfo:
                user, _, _ = userinfo.partition(":")
                safe_userinfo = f"{user}:***"
            else:
                # User but no password — preserve as-is, do not fabricate a marker
                safe_userinfo = userinfo
            safe_netloc = f"{safe_userinfo}@{host}" if safe_userinfo else host
            return urlunparse(parsed._replace(netloc=safe_netloc))

        # Well-formed URL (both scheme and netloc present) with no credentials
        # in netloc — any `@` in the raw string is in path/query/fragment, which
        # is not a credential (e.g. "https://example.com/path@v1" or a query
        # with "?email=a@b.com"). Pass through unchanged.
        if parsed.scheme and parsed.netloc:
            return url

        # urlparse did not produce a well-formed URL (scheme or netloc missing).
        # Fail closed: return the placeholder. This covers scheme-less
        # credential strings like "user:pw@host" (where the password ends up
        # in `path`), half-formed inputs like "not a url at all", and any raw
        # token/secret that a caller accidentally passes instead of a URL.
        return _REDACTED
    except (ValueError, TypeError):
        return _REDACTED
