"""``details.observed_at`` contract: when a fact held (pure logic, no I/O, #2330).

A memory's ``created_at`` / ``updated_at`` are write times. ``observed_at`` is
the time the fact held or the event happened, written by the caller into
``details.observed_at`` (or the top-level ``observed_at`` argument, which lands
there). The STORED generated column ``memories.observed_at`` is derived from
it (``models/memory.py``, migration ``f16_2330_observed_at``), the way
``details.location`` drives ``location_lat`` / ``location_lon``.

Forms:

- **Stored in details**: a date ``YYYY-MM-DD`` or a UTC datetime
  ``YYYY-MM-DDTHH:MM:SSZ``. The writer's precision is kept, so an exact
  ``details.observed_at == "2024-03-07"`` filter keeps matching.
- **Column / payload value**: always the 20-char datetime; a date is padded to
  ``T00:00:00Z`` (the start of that day, UTC). One fixed width keeps lexical
  order equal to chronological order under any collation.
- **Accepted input**: ISO 8601 extended forms only — a date ``YYYY-MM-DD``
  or a datetime ``YYYY-MM-DDTHH:MM:SS`` with optional fractional seconds and
  an optional ``Z`` / ``±HH:MM`` offset. A naive datetime is UTC, an aware one
  is converted to UTC, fractional seconds are dropped. Anything else — basic
  forms such as ``20240307``, week dates, a space separator, a time without
  seconds — is rejected (a 422 / ``validation_error``, never a silent NULL),
  so a stored date is always ``YYYY-MM-DD`` and an exact date filter finds
  it. ``None`` removes the key.

One normaliser, four readers: :func:`normalize_observed_at` (every write path,
via ``MemoryService._apply_observed_at``), :func:`column_value` (the Qdrant
payload of a metadata edit), :func:`observed_at_from_details` (responses) and
:func:`parse_observed_bound` (filters).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

# Regex literals of the generated column (models/memory.py). A parity test
# keeps them equal to the SQL text.
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
DATETIME_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
MAX_INPUT_CHARS = 40

# Readers use ``fullmatch``: ``$`` alone would also accept a trailing newline,
# which the SQL regex (``$`` = end of string) does not.
_DATE = re.compile(DATE_RE)
_DATETIME = re.compile(DATETIME_RE)
# The input forms accepted on write (before the calendar check).
_INPUT_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})?"
)

_FORMAT_HINT = (
    "observed_at must be an ISO 8601 date (2024-03-07) or datetime (2024-03-07T10:00:00Z)"
)


class ObservedAtValidationError(ValueError):
    """A caller-supplied ``observed_at`` that is not an ISO 8601 date or datetime.

    Subclasses ``ValueError`` so the MCP ``validation_error`` / REST 422
    mappings apply as for ``LocationValidationError``.
    """


def canonical_observed_at(value: Any) -> str:
    """The stored form of one ``observed_at`` value.

    Returns ``YYYY-MM-DD`` for a date and ``YYYY-MM-DDTHH:MM:SSZ`` (UTC) for a
    datetime.

    Raises:
        ObservedAtValidationError: not a string, too long, or not ISO 8601.
    """
    # bool is not a str, but say so explicitly: numbers and booleans are
    # rejected rather than coerced.
    if isinstance(value, bool) or not isinstance(value, str):
        raise ObservedAtValidationError(f"{_FORMAT_HINT}, got {type(value).__name__}")
    text = value.strip()
    if not text or len(text) > MAX_INPUT_CHARS:
        raise ObservedAtValidationError(_FORMAT_HINT)
    try:
        if _DATE.fullmatch(text):
            day = date.fromisoformat(text)
            return f"{day.year:04d}-{day.month:02d}-{day.day:02d}"
        if not _INPUT_DATETIME.fullmatch(text):
            raise ValueError(text)
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        else:
            parsed = parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        # OverflowError: an offset that moves year 1 / 9999 out of range.
        raise ObservedAtValidationError(_FORMAT_HINT) from exc
    # Explicit zero padding: strftime("%Y") does not pad years below 1000 on
    # every platform, and the column regex needs four digits.
    return (
        f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}"
        f"T{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}Z"
    )


def normalize_observed_at(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate ``details.observed_at`` and write the stored form back.

    Gate: fires only when the ``observed_at`` key is present (any memory type
    may carry it). Absent key or ``None`` details pass through untouched; a
    ``None`` value removes the key.

    Raises:
        ObservedAtValidationError: the value is not an ISO 8601 date or datetime.
    """
    if details is None or "observed_at" not in details:
        return details
    value = details["observed_at"]
    if value is None:
        details.pop("observed_at")
        return details
    details["observed_at"] = canonical_observed_at(value)
    return details


def column_value(details: Any) -> str | None:
    """The 20-char value the generated column holds for ``details``, or None.

    The Python twin of the column's SQL ``CASE``: a date is padded with
    ``T00:00:00Z``, a UTC datetime is kept, anything else is None. Never
    raises.

    Payload-source invariant (the ``location_payload_from_details`` rule):
    the update/patch/rollback/erasure payload patch calls this on the edited
    details because the ORM row has not round-tripped the generated column
    yet; ``build_memory_point`` reads ``Memory.observed_at``. The two agree
    because ``normalize_observed_at`` ran on caller-supplied details.
    """
    if not isinstance(details, dict):
        return None
    value = details.get("observed_at")
    if not isinstance(value, str):
        return None
    if _DATE.fullmatch(value):
        return value + "T00:00:00Z"
    if _DATETIME.fullmatch(value):
        return value
    return None


def observed_at_from_details(details: Any) -> str | None:
    """The ``observed_at`` a response shows: the stored form, or None.

    A legacy value that is not in a stored form (written before the
    contract) is None, matching the generated column. Never raises.
    """
    if not isinstance(details, dict):
        return None
    value = details.get("observed_at")
    if isinstance(value, str) and (_DATE.fullmatch(value) or _DATETIME.fullmatch(value)):
        return value
    return None


def parse_observed_bound(value: Any, name: str) -> tuple[str, datetime]:
    """A filter bound (``observed_after`` / ``observed_before``).

    A date means the start of that day, UTC.

    Returns:
        ``(column form, aware UTC datetime)``.

    Raises:
        ObservedAtValidationError: not an ISO 8601 date or datetime string.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ObservedAtValidationError(f"{name} must be an ISO 8601 date or datetime string")
    try:
        canonical = canonical_observed_at(value)
    except ObservedAtValidationError as exc:
        raise ObservedAtValidationError(
            f"{name} must be an ISO 8601 date (2024-03-07) or datetime (2024-03-07T10:00:00Z)"
        ) from exc
    column = column_value({"observed_at": canonical})
    if column is None:  # pragma: no cover - canonical forms always match
        raise ObservedAtValidationError(f"{name} must be an ISO 8601 date or datetime string")
    return column, datetime.fromisoformat(column)
