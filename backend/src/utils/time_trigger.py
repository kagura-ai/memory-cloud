"""Time Memory trigger derivation (pure logic, no I/O, no LLM).

A Time Memory (``type="time"``) stores fuzzy Y/M/D[/H/M] components supplied by
the caller. ``normalize_trigger`` validates them and derives the canonical
``[from, until]`` window that the generated columns ``trigger_from`` /
``trigger_until`` index. The deepest non-null component defines the precision
and therefore the width of the window.

Window bounds are emitted as **naive ISO strings without a Z suffix** so the
PostgreSQL ``(details->'trigger'->>'from')::timestamp`` generated-column cast is
unambiguous (the columns are ``TIMESTAMP WITHOUT TIME ZONE``, naive UTC by
convention).

This module constructs explicit naive ``datetime`` values for deterministic
window arithmetic — it never reads a wall clock — so flake8-datetimez's DTZ001
concern (an accidental local-tz ``datetime.now()``) does not apply here.
"""
# ruff: noqa: DTZ001 — deterministic naive window math, no wall-clock now()

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

_ISO = "%Y-%m-%dT%H:%M:%S"


class TriggerValidationError(ValueError):
    """Raised when caller-supplied trigger components are invalid."""


def _days_in_month(year: int, month: int) -> int:
    first_next = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return (first_next - timedelta(days=1)).day


def normalize_trigger(trigger: Any) -> dict:
    """Validate Y/M/D[/H/M] components and return a dict with the derived window.

    Returns a new dict carrying the echoed components (``year``/``month``/``day``/
    ``hour``/``minute``, missing levels as ``None``), any ``text`` passed through,
    and the derived ``from``/``until`` ISO strings.

    Raises:
        TriggerValidationError: on a non-dict, missing/out-of-range year, an
            impossible date, or a skipped precision level (e.g. day without month).
    """
    if not isinstance(trigger, dict):
        raise TriggerValidationError("trigger must be an object")

    year = trigger.get("year")
    month = trigger.get("month")
    day = trigger.get("day")
    hour = trigger.get("hour")
    minute = trigger.get("minute")

    if not isinstance(year, int) or isinstance(year, bool):
        raise TriggerValidationError("trigger.year is required and must be an integer")
    if not 1 <= year <= 9999:
        raise TriggerValidationError("trigger.year out of range (1-9999)")

    # Precision must not skip levels.
    if month is None and any(v is not None for v in (day, hour, minute)):
        raise TriggerValidationError("trigger.month required when day/hour/minute set")
    if day is None and any(v is not None for v in (hour, minute)):
        raise TriggerValidationError("trigger.day required when hour/minute set")
    if minute is not None and hour is None:
        raise TriggerValidationError("trigger.hour required when minute set")

    if month is not None and not 1 <= month <= 12:
        raise TriggerValidationError("trigger.month out of range (1-12)")
    if day is not None and not 1 <= day <= _days_in_month(year, month):
        raise TriggerValidationError("trigger.day out of range for the given month")
    if hour is not None and not 0 <= hour <= 23:
        raise TriggerValidationError("trigger.hour out of range (0-23)")
    if minute is not None and not 0 <= minute <= 59:
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
    result["from"] = frm.strftime(_ISO)
    result["until"] = until.strftime(_ISO)
    return result


def format_trigger_label(trigger: dict) -> str:
    """Precision-aware display label derived from the components.

    Year/month precision renders the fuzzy "ごろ" form; day and finer render the
    exact date/time.
    """
    year = trigger.get("year")
    month = trigger.get("month")
    day = trigger.get("day")
    hour = trigger.get("hour")
    minute = trigger.get("minute")
    if month is None:
        return f"{year}年ごろ"
    if day is None:
        return f"{year}年{month}月ごろ"
    if hour is None:
        return f"{year:04d}-{month:02d}-{day:02d}"
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute or 0:02d}"
