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


def parse_iso8601_to_aware(value: str, field_name: str) -> datetime:
    """Parse an ISO 8601 string to a timezone-aware datetime.

    Args:
        value: ISO 8601 datetime string (e.g. "2026-03-01T00:00:00Z")
        field_name: Field name for error messages

    Returns:
        Timezone-aware datetime (naive inputs are assumed UTC)

    Raises:
        ValueError: If the string is not a valid ISO 8601 datetime
    """
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid datetime for {field_name}: {value!r}") from e
