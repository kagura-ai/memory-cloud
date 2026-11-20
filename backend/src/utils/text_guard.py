"""Guards for request text that is about to reach PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterable


def has_nul(pairs: Iterable[tuple[str, str]]) -> bool:
    """Whether any key or value holds a NUL byte (#2946).

    PostgreSQL text cannot store a NUL, so the driver raises on such a query
    parameter and a catch-all handler would answer 503. Callers refuse the
    request as malformed before any lookup instead.

    Args:
        pairs: Key/value pairs of a parsed form or query string.

    Returns:
        True when a key or a value contains ``\\x00``.
    """
    return any("\x00" in key or "\x00" in value for key, value in pairs)
