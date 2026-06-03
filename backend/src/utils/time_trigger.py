"""Time Memory trigger derivation (pure logic, no I/O, no LLM).

A Time Memory (``type="time"``) stores fuzzy Y/M/D[/H/M] components supplied by
the caller. ``normalize_trigger`` validates them and derives the canonical
``[from, until]`` window that the generated columns ``trigger_from`` /
``trigger_until`` index. The deepest non-null component defines the precision
and therefore the width of the window.

Window bounds are emitted as **fixed-width, zero-padded naive ISO strings
without a Z suffix** (e.g. ``2026-07-01T00:00:00``). The generated columns
``trigger_from`` / ``trigger_until`` are TEXT — a plain ``details->'trigger'->>
'from'`` extraction is IMMUTABLE, whereas a ``::timestamp`` cast is only STABLE
and PostgreSQL rejects it in a STORED generated column. The window-overlap
filter and ``ORDER BY trigger_from`` therefore rely on **lexical order matching
chronological order**, which holds only for this fixed-width zero-padded form —
hence ``parse_query_bound`` re-normalizes every query bound to the same shape
before it touches the column.

This module constructs explicit naive ``datetime`` values for deterministic
window arithmetic — it never reads a wall clock except in ``parse_query_bound``
for the ``'now'`` shortcut — so flake8-datetimez's DTZ001 concern (an accidental
local-tz ``datetime.now()``) does not apply to the constructors here.
"""
# ruff: noqa: DTZ001 — deterministic naive window math, no wall-clock now()

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any

from utils.datetime import utcnow


class TriggerValidationError(ValueError):
    """Raised when caller-supplied trigger components are invalid."""


def _iso(dt: datetime) -> str:
    """Format a naive datetime as fixed-width zero-padded ISO without a Z suffix.

    The year is explicitly zero-padded to 4 digits (unlike ``strftime('%Y')`` on
    some libc builds) so the emitted strings are fixed-width — the load-bearing
    invariant behind the lexical==chronological comparison (see module docstring).
    """
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    )


def _require_int(value: Any, name: str) -> int:
    """Return ``value`` as an int, rejecting bool and non-int types.

    ``bool`` is a subclass of ``int`` in Python (``True == 1``), so a bare
    ``isinstance(x, int)`` would silently accept ``True``/``False`` as 1/0.
    A float (``7.0``) would pass a range check but then crash inside the
    ``datetime`` constructor with a TypeError that escapes the caller's
    ``TriggerValidationError`` handler — so reject anything that is not a plain
    int here, where the error maps cleanly to a 4xx.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TriggerValidationError(f"trigger.{name} must be an integer")
    return value


def _days_in_month(year: int, month: int) -> int:
    """Last day of the given month. Uses ``calendar`` so December does not
    overflow ``datetime`` (year 9999 + 1 would raise)."""
    return calendar.monthrange(year, month)[1]


def normalize_trigger(trigger: Any) -> dict:
    """Validate Y/M/D[/H/M] components and return a dict with the derived window.

    Returns a new dict carrying the echoed components (``year``/``month``/``day``/
    ``hour``/``minute``, missing levels as ``None``), any ``text`` passed through,
    and the derived ``from``/``until`` ISO strings.

    Raises:
        TriggerValidationError: on a non-dict, a non-integer/bool component, a
            missing/out-of-range year, an impossible date, or a skipped precision
            level (e.g. day without month).
    """
    if not isinstance(trigger, dict):
        raise TriggerValidationError("trigger must be an object")

    year = trigger.get("year")
    month = trigger.get("month")
    day = trigger.get("day")
    hour = trigger.get("hour")
    minute = trigger.get("minute")

    if year is None:
        raise TriggerValidationError("trigger.year is required")
    year = _require_int(year, "year")
    if not 1 <= year <= 9999:
        raise TriggerValidationError("trigger.year out of range (1-9999)")

    # Precision must not skip levels.
    if month is None and any(v is not None for v in (day, hour, minute)):
        raise TriggerValidationError("trigger.month required when day/hour/minute set")
    if day is None and any(v is not None for v in (hour, minute)):
        raise TriggerValidationError("trigger.day required when hour/minute set")
    if minute is not None and hour is None:
        raise TriggerValidationError("trigger.hour required when minute set")

    # Type + range validation (reject bool/float before any datetime math).
    if month is not None:
        month = _require_int(month, "month")
        if not 1 <= month <= 12:
            raise TriggerValidationError("trigger.month out of range (1-12)")
    if day is not None:
        day = _require_int(day, "day")
        if not 1 <= day <= _days_in_month(year, month):
            raise TriggerValidationError("trigger.day out of range for the given month")
    if hour is not None:
        hour = _require_int(hour, "hour")
        if not 0 <= hour <= 23:
            raise TriggerValidationError("trigger.hour out of range (0-23)")
    if minute is not None:
        minute = _require_int(minute, "minute")
        if not 0 <= minute <= 59:
            raise TriggerValidationError("trigger.minute out of range (0-59)")

    if month is None:
        frm = datetime(year, 1, 1, 0, 0, 0)
        until = datetime(year, 12, 31, 23, 59, 59)
    elif day is None:
        frm = datetime(year, month, 1, 0, 0, 0)
        until = datetime(year, month, _days_in_month(year, month), 23, 59, 59)
    elif hour is None:
        frm = datetime(year, month, day, 0, 0, 0)
        until = datetime(year, month, day, 23, 59, 59)
    elif minute is None:
        frm = datetime(year, month, day, hour, 0, 0)
        until = datetime(year, month, day, hour, 59, 59)
    else:
        frm = datetime(year, month, day, hour, minute, 0)
        until = datetime(year, month, day, hour, minute, 59)

    result = dict(trigger)
    result["year"] = year
    result["month"] = month
    result["day"] = day
    result["hour"] = hour
    result["minute"] = minute
    result["from"] = _iso(frm)
    result["until"] = _iso(until)
    return result


def parse_query_bound(value: Any) -> str | None:
    """Normalize a recall window bound to a fixed-width ISO string (or None).

    A window bound (``trigger_from`` / ``trigger_until`` on GET /memory/list,
    ``from`` / ``until`` on the recall_upcoming MCP tool) is compared
    **lexically** against the stored TEXT columns, so it MUST be re-emitted in
    the same fixed-width zero-padded ISO form — otherwise a caller passing
    ``2026-7-1T00:00:00`` (not zero-padded) would silently match the wrong rows.

    - ``None`` → ``None`` (no bound).
    - ``"now"`` (case-insensitive) → current UTC time as fixed-width ISO. This
      resolves a *read* query bound only (never stored data), so it does not
      reintroduce the server-side clock the storage path deliberately avoids.
    - any other value → parsed with ``datetime.fromisoformat`` (which rejects
      non-zero-padded / malformed input) and re-emitted via ``_iso``; aware
      datetimes are converted to naive UTC to match the naive-UTC storage.

    Raises:
        TriggerValidationError: if the value is not ``None``/``"now"`` and is not
            a parseable ISO datetime.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() == "now":
        return _iso(utcnow())
    if not isinstance(value, str):
        raise TriggerValidationError("time bound must be an ISO datetime string or 'now'")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TriggerValidationError(
            f"invalid time bound {value!r}: expected a zero-padded ISO datetime "
            "(e.g. 2026-07-01T00:00:00) or 'now'"
        ) from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return _iso(dt)


def format_trigger_label(trigger: dict) -> str:
    """Precision-aware English display label derived from the components.

    A fuzzy level carries a parenthetical granularity marker so the label
    communicates the *width* of the window, not a false exactness:
    year → "2026 (year)", month → "2026-07 (month)", day → "2026-07-15",
    hour → "2026-07-15 10:00 (hour)", minute → "2026-07-15 10:30".

    Note the hour vs. minute distinction: an hour-precision trigger spans the
    whole hour ([HH:00:00, HH:59:59]), so it carries the "(hour)" marker — a
    bare ``HH:00`` would be indistinguishable from a minute-precision trigger at
    ``:00`` and imply a 1-minute window it does not have.

    Display strings are kept English/locale-neutral here; any localized
    rendering belongs in the presentation layer, not this backend util.
    """
    year = trigger.get("year")
    month = trigger.get("month")
    day = trigger.get("day")
    hour = trigger.get("hour")
    minute = trigger.get("minute")
    if month is None:
        return f"{year} (year)"
    if day is None:
        return f"{year:04d}-{month:02d} (month)"
    if hour is None:
        return f"{year:04d}-{month:02d}-{day:02d}"
    if minute is None:
        return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:00 (hour)"
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
