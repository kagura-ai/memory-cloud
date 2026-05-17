"""Timezone-safe datetime utilities.

Provides utcnow() as a drop-in replacement for the deprecated
datetime.utcnow(), returning naive UTC datetimes that map cleanly to the
project's TIMESTAMP WITHOUT TIME ZONE columns.

The project intentionally stores naive UTC in the DB; UTC is enforced
explicitly by three layers (postgres container ``TZ``/``PGTZ`` env vars,
async/sync engine ``connect_args`` pinning the session timezone, and
Python writes via ``utcnow()`` here). See ``.claude/rules/backend.md``
for the full convention.
"""

from datetime import UTC, datetime


def to_utc_iso(dt: datetime | None) -> str | None:
    """Serialize a datetime as an ISO 8601 string with explicit Z suffix.

    Naive datetimes are treated as UTC (the project convention for
    TIMESTAMP WITHOUT TIME ZONE columns). Without the Z suffix, JS
    clients parse the string as local time and display the wrong offset.

    Args:
        dt: Datetime to serialize (may be None)

    Returns:
        ISO 8601 string with Z suffix, or None if input is None
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat().replace("+00:00", "Z")


def utcnow() -> datetime:
    """Return current UTC time as a naive datetime.

    Equivalent to the deprecated datetime.utcnow() but uses the
    recommended datetime.now(UTC) internally.

    Returns naive datetime to match the project's TIMESTAMP WITHOUT TIME
    ZONE columns. The "naive" value is unambiguously UTC because the
    container OS, the PostgreSQL session, and this function all pin to
    UTC — see ``.claude/rules/backend.md`` for the three-layer policy.

    Wire-format Z-suffix for API responses is handled at the schema
    layer by ``TZAwareBaseModel`` and ``to_utc_iso`` — do not append a
    ``Z`` here.

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
