"""Timezone-safe datetime utilities.

Provides utcnow() as a drop-in replacement for the deprecated
datetime.utcnow(), returning naive UTC datetimes for compatibility
with existing TIMESTAMP WITHOUT TIME ZONE database columns.
"""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return current UTC time as a naive datetime.

    Equivalent to the deprecated datetime.utcnow() but uses the
    recommended datetime.now(UTC) internally.

    Returns naive datetime for compatibility with PostgreSQL
    TIMESTAMP WITHOUT TIME ZONE columns. When the database is
    migrated to TIMESTAMP WITH TIME ZONE, this function should
    be updated to return timezone-aware datetimes.

    Returns:
        Naive datetime representing current UTC time
    """
    return datetime.now(UTC).replace(tzinfo=None)
