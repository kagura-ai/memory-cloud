"""``details.event_phrase`` → ``details.event_start`` / ``event_end`` (pure logic, no I/O).

A memory's ``observed_at`` is the date of the material: the day a fact was
stated. The material often places the event itself relative to that day
("last weekend", "the last week of August", "two weeks ago"). Resolving such a
phrase in the writing model kept going wrong in the same ways (a range
snapped to one day, "yesterday" kept as the day it was said, a year off across
a year boundary), so the server resolves it, deterministically, at write time:

- The writer passes the phrase as stated in ``details.event_phrase`` and the
  material's date in ``details.observed_at`` (whose meaning does not change).
- :func:`normalize_event_dates` (every write path, via
  ``MemoryService._apply_event_dates``) keeps the phrase verbatim and, when it
  resolves against the ``observed_at`` day, writes ``details.event_start`` /
  ``details.event_end`` as ``YYYY-MM-DD`` (a single day has equal ends). A
  phrase outside the rule set, or one without an ``observed_at`` to count
  from, leaves both keys absent: the server never guesses.
- A client that passes ``event_start`` / ``event_end`` itself, without a
  phrase, keeps them as given (validated as calendar dates; ``event_end``
  defaults to ``event_start``).
- ``details.event_direction`` (optional, ``"past"`` or ``"future"``, stored
  lower-cased and stripped next to the phrase) says whether a bare weekday
  names a past or an upcoming day; the server cannot tell from the phrase.

Rule set (:func:`resolve_event_phrase`; ``anchor`` is the ``observed_at`` day):

- ``today``, ``yesterday``, ``tomorrow``; ``tonight``, ``this morning``,
  ``this afternoon``, ``this evening`` (the anchor); ``last night`` (the day
  before); ``the day after tomorrow`` / ``the day before yesterday`` (two days
  after / before).
- ``N days ago`` (the day), ``N weeks ago`` (the day ``7 N`` days back),
  ``N months ago`` (the day ``N`` months back, clamped to the month's end),
  ``N years ago`` (the same calendar day ``N`` years back, clamped the same
  way: 29 February is 28 February in a year without one; before year 1 it is
  unresolved); ``N`` as digits or the words one … twelve, ``a`` / ``an``
  meaning one.
- ``last`` / ``this`` / ``next`` ``week`` (Mon–Sun), ``weekend`` (Sat–Sun),
  ``month``, ``year``. Weeks are counted from the anchor's weekday: "this
  week" is the Mon–Sun week holding the anchor, "last weekend" the Sat–Sun
  just before the anchor (on a Saturday or Sunday, the weekend before the
  current one), "this weekend" the Sat–Sun of the anchor's week.
- ``N weekends ago``: ``last weekend`` is one weekend ago; each further
  weekend is seven days earlier.
- ``first week of <month>`` / ``last week of <month>``: the Mon–Sun week
  holding the month's first / last Thursday (the week with at least four of
  its days in that month, as printed calendars count weeks), so it may begin
  in the month before or end in the month after.
- ``<month>``: the whole month, the most recent one that started on or before
  the anchor ("December" from 2026-01-09 is December 2025).
- ``<weekday>`` (optionally ``last <weekday>``): the most recent one strictly
  before the anchor. With ``details.event_direction = "future"`` a BARE
  weekday ("Sunday", "on Sunday") is instead the first such day on or after
  the anchor (the anchor itself when it is that weekday); ``"past"`` or no
  direction keeps the rule above. ``last <weekday>``, ``this / next
  <weekday>`` and every other phrase ignore the direction.
- ``this <weekday>``: that weekday in the Mon–Sun week holding the anchor (on,
  before or after it). ``next <weekday>``: that weekday in the Mon–Sun week
  after the anchor's week (from Saturday 2024-03-09, "this Sunday" is
  2024-03-10, "next Sunday" 2024-03-17 and "next Monday" 2024-03-11).

Matching is case-insensitive, tolerant of surrounding whitespace, a trailing
period and a leading ``on`` / ``in`` / ``the``. Anything else is unresolved.

Responses: :func:`event_fields` puts ``event_start``, ``event_end`` and
``event_weekday`` (the weekday of ``event_start``) on a row or a write
response when the memory has them; :func:`event_write_fields` adds
``event_unresolved`` (``no_observed_at`` or ``unknown_phrase``) to a write
response whose phrase did not resolve.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

from utils.observed_at import DATE_RE, WEEKDAYS, ObservedAtValidationError, canonical_observed_at

# Regex literal of the generated columns ``memories.event_start`` /
# ``event_end`` (models/memory.py); a parity test keeps them equal.
EVENT_DATE_RE = DATE_RE
MAX_PHRASE_CHARS = 100
# The recall / list filter keys over the event's own days: a row matches when
# its ``[event_start, event_end]`` overlaps ``[event_after, event_before)``.
EVENT_FILTER_KEYS = ("event_after", "event_before")
_PAYLOAD_SUFFIX = "T00:00:00Z"

_EVENT_DATE = re.compile(EVENT_DATE_RE)
_WORD_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})
_MONTHS["sept"] = 9
_WEEKDAYS = {name.lower(): number for number, name in enumerate(calendar.day_name)}
_WEEKDAYS.update({name.lower(): number for number, name in enumerate(calendar.day_abbr)})
_LEADING = ("on", "in", "the")
_MAX_UNITS = 1_000  # "1000 days ago" is as far as a sentence goes; beyond is a typo

_N_AGO = re.compile(r"(?P<n>\d{1,4}|[a-z]+) (?P<unit>day|week|month|weekend|year)s? ago")
_RELATIVE_PERIOD = re.compile(r"(?P<which>last|this|next) (?P<unit>weekend|week|month|year)")
_WEEK_OF_MONTH = re.compile(r"(?P<which>first|last) week of (?P<month>[a-z]+)")
_THIS_NEXT_WEEKDAY = re.compile(r"(?P<which>this|next) (?P<day>[a-z]+)")
_LAST_WEEKDAY = re.compile(r"(?:last )?(?P<day>[a-z]+)")
# Fixed phrases: days counted from the anchor.
_DAY_OFFSETS = {
    "today": 0,
    "tonight": 0,
    "this morning": 0,
    "this afternoon": 0,
    "this evening": 0,
    "yesterday": -1,
    "last night": -1,
    "tomorrow": 1,
    "day after tomorrow": 2,
    "day before yesterday": -2,
}

_FORMAT_HINT = "event_start / event_end must be ISO 8601 dates (2024-03-07)"
EVENT_DIRECTIONS = ("past", "future")
_DIRECTION_HINT = 'event_direction must be "past" or "future"'


class EventDateValidationError(ValueError):
    """A caller-supplied ``event_phrase`` / ``event_start`` / ``event_end`` the server refuses.

    Subclasses ``ValueError`` so the MCP ``validation_error`` / REST 422
    mappings apply as for ``ObservedAtValidationError``.
    """


# ------------------------------------------------------------------ calendar


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _shift_months(day: date, months: int) -> date:
    """``day`` moved ``months`` back (positive) or forward (negative), day clamped."""
    index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _last_weekend(anchor: date) -> tuple[date, date]:
    """The Sat–Sun just before the anchor's week (on Sat / Sun: the one before)."""
    saturday = _monday(anchor) - timedelta(days=2)
    return saturday, saturday + timedelta(days=1)


def _week_of_month(year: int, month: int, which: str) -> tuple[date, date]:
    """The Mon–Sun week holding the month's first (``first``) or last Thursday."""
    if which == "first":
        day = _month_start(year, month)
        while day.weekday() != 3:
            day += timedelta(days=1)
    else:
        day = _month_end(year, month)
        while day.weekday() != 3:
            day -= timedelta(days=1)
    monday = _monday(day)
    return monday, monday + timedelta(days=6)


def _recent_month(anchor: date, month: int) -> tuple[date, date]:
    """The most recent ``month`` that started on or before ``anchor``."""
    year = anchor.year if month <= anchor.month else anchor.year - 1
    return _month_start(year, month), _month_end(year, month)


def _count(token: str) -> int | None:
    if token.isdigit():
        n = int(token)
    else:
        n = _WORD_NUMBERS.get(token, 0)
    return n if 0 < n <= _MAX_UNITS else None


def _normalize(phrase: str) -> str:
    words = phrase.lower().replace("-", " ").split()
    while words and words[0] in _LEADING:
        words = words[1:]
    text = " ".join(words)
    return text.rstrip(".,!").strip()


def resolve_event_phrase(
    phrase: str, anchor: date, direction: str | None = None
) -> tuple[date, date] | None:
    """The ``(start, end)`` days ``phrase`` names, counted from ``anchor``, or None.

    Deterministic: the rule set in the module docstring and nothing else; no
    clock is read. ``None`` means the phrase is outside the rules.

    Args:
        phrase: The relative date as stated ("last weekend", "two weeks ago").
        anchor: The material's date (the ``observed_at`` day).
        direction: ``details.event_direction``. ``"future"`` makes a bare
            weekday the first such day on or after ``anchor``; anything else
            (``"past"``, None) keeps the most recent one before it. Every
            other phrase ignores it.
    """
    if not isinstance(phrase, str) or not isinstance(anchor, date):
        return None
    text = _normalize(phrase)
    if not text:
        return None
    if (offset := _DAY_OFFSETS.get(text)) is not None:
        day = anchor + timedelta(days=offset)
        return day, day

    if (m := _N_AGO.fullmatch(text)) is not None:
        n = _count(m.group("n"))
        if n is None:
            return None
        unit = m.group("unit")
        if unit == "day":
            day = anchor - timedelta(days=n)
            return day, day
        if unit == "week":
            day = anchor - timedelta(days=7 * n)
            return day, day
        if unit in ("month", "year"):
            try:
                day = _shift_months(anchor, n * 12 if unit == "year" else n)
            except ValueError:  # before year 1
                return None
            return day, day
        saturday, sunday = _last_weekend(anchor)
        back = timedelta(days=7 * (n - 1))
        return saturday - back, sunday - back

    if (m := _RELATIVE_PERIOD.fullmatch(text)) is not None:
        which, unit = m.group("which"), m.group("unit")
        offset = {"last": -1, "this": 0, "next": 1}[which]
        if unit == "week":
            monday = _monday(anchor) + timedelta(days=7 * offset)
            return monday, monday + timedelta(days=6)
        if unit == "weekend":
            if which == "last":
                return _last_weekend(anchor)
            saturday = _monday(anchor) + timedelta(days=5 + 7 * offset)
            return saturday, saturday + timedelta(days=1)
        if unit == "month":
            try:
                first = _shift_months(_month_start(anchor.year, anchor.month), -offset)
            except ValueError:
                return None
            return first, _month_end(first.year, first.month)
        year = anchor.year + offset
        try:
            return date(year, 1, 1), date(year, 12, 31)
        except ValueError:
            return None

    if (m := _WEEK_OF_MONTH.fullmatch(text)) is not None:
        month = _MONTHS.get(m.group("month"))
        if month is None:
            return None
        start, _ = _recent_month(anchor, month)
        return _week_of_month(start.year, start.month, m.group("which"))

    if (month := _MONTHS.get(text)) is not None:
        return _recent_month(anchor, month)

    if (m := _THIS_NEXT_WEEKDAY.fullmatch(text)) is not None:
        weekday = _WEEKDAYS.get(m.group("day"))
        if weekday is None:
            return None
        week = 7 if m.group("which") == "next" else 0
        day = _monday(anchor) + timedelta(days=week + weekday)
        return day, day

    if (m := _LAST_WEEKDAY.fullmatch(text)) is not None:
        weekday = _WEEKDAYS.get(m.group("day"))
        if weekday is None:
            return None
        if direction == "future" and not text.startswith("last "):
            day = anchor + timedelta(days=(weekday - anchor.weekday()) % 7)
            return day, day
        back = (anchor.weekday() - weekday) % 7 or 7
        day = anchor - timedelta(days=back)
        return day, day
    return None


def resolved_event_days(details: Any) -> tuple[date, date] | None:
    """The days the stored ``event_phrase`` resolves to from the stored ``observed_at``
    (and ``event_direction``), or None: no phrase, no anchor or outside the rules.

    For readers that need to know the dates came from the phrase (the
    ``event_date_mismatch`` write lint), not from a pair the client passed.
    Never raises.
    """
    if not isinstance(details, dict):
        return None
    phrase = details.get("event_phrase")
    anchor = _anchor(details)
    if not isinstance(phrase, str) or anchor is None:
        return None
    direction = details.get("event_direction")
    try:
        return resolve_event_phrase(
            phrase, anchor, direction if isinstance(direction, str) else None
        )
    except (OverflowError, ValueError):
        return None


# --------------------------------------------------------------- write path


def _calendar_date(value: Any, key: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise EventDateValidationError(f"{_FORMAT_HINT}, got {type(value).__name__} for {key}")
    text = value.strip()
    if not _EVENT_DATE.fullmatch(text):
        raise EventDateValidationError(f"{_FORMAT_HINT}; {key} is {value!r}")
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise EventDateValidationError(f"{_FORMAT_HINT}; {key} is not a calendar date") from exc
    return day.isoformat()


def _direction(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise EventDateValidationError(_DIRECTION_HINT)
    text = value.strip().lower()
    if text not in EVENT_DIRECTIONS:
        raise EventDateValidationError(_DIRECTION_HINT)
    return text


def _anchor(details: dict[str, Any]) -> date | None:
    """The ``observed_at`` day of normalised details (a date or a UTC datetime), or None."""
    value = details.get("observed_at")
    if not isinstance(value, str) or not _EVENT_DATE.fullmatch(value[:10]):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def normalize_event_dates(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate the event keys of caller-supplied details and resolve the phrase.

    Runs after ``normalize_observed_at`` (the anchor is the stored
    ``observed_at``). Gate: fires only when one of ``event_phrase``,
    ``event_start``, ``event_end``, ``event_direction`` is present; absent keys
    or ``None`` details pass through untouched. A ``None`` value removes its
    key. ``event_direction`` is stored lower-cased and stripped and only
    changes a bare weekday phrase (see :func:`resolve_event_phrase`).

    With a phrase that resolves against the ``observed_at`` day the server
    owns both dates (a re-sent pair never outlives a changed phrase). A phrase
    that does not resolve (outside the rule set, no ``observed_at``, or a date
    outside the calendar) leaves an explicit pair the caller supplied, so
    dates the writer knew are not lost; without one the dates stay absent.
    Without a phrase an explicit pair is validated and kept (``event_end``
    defaults to ``event_start``).

    Raises:
        EventDateValidationError: a phrase that is not a non-empty string of at
            most ``MAX_PHRASE_CHARS``, a date that is not ``YYYY-MM-DD``, an
            ``event_end`` without ``event_start``, an end before its start, or
            an ``event_direction`` other than ``"past"`` / ``"future"``.
    """
    if details is None:
        return details
    keys = {"event_phrase", "event_start", "event_end", "event_direction"} & details.keys()
    if not keys:
        return details
    for key in keys:
        if details[key] is None:
            details.pop(key)
    if "event_direction" in details:
        details["event_direction"] = _direction(details["event_direction"])
    phrase = details.get("event_phrase")
    if "event_phrase" in details:
        if isinstance(phrase, bool) or not isinstance(phrase, str) or not phrase.strip():
            raise EventDateValidationError("event_phrase must be a non-empty string")
        if len(phrase) > MAX_PHRASE_CHARS:
            raise EventDateValidationError(f"event_phrase is longer than {MAX_PHRASE_CHARS} chars")
        anchor = _anchor(details)
        try:
            resolved = (
                resolve_event_phrase(phrase, anchor, details.get("event_direction"))
                if anchor is not None
                else None
            )
        except OverflowError:
            # The arithmetic left the calendar (an anchor at year 1 or 9999):
            # unresolved, like any phrase the server cannot count.
            resolved = None
        if resolved is not None:
            details["event_start"] = resolved[0].isoformat()
            details["event_end"] = resolved[1].isoformat()
            return details
        if "event_start" not in details:
            details.pop("event_end", None)
            return details
        # Unresolved with an explicit pair: validate and keep it, below.
    if "event_end" in details and "event_start" not in details:
        raise EventDateValidationError("event_end needs event_start")
    if "event_start" in details:
        start = _calendar_date(details["event_start"], "event_start")
        end = _calendar_date(details["event_end"], "event_end") if "event_end" in details else start
        if end < start:
            raise EventDateValidationError("event_end is before event_start")
        details["event_start"] = start
        details["event_end"] = end
    return details


# ----------------------------------------------------------------- readers


def event_dates_from_details(details: Any) -> tuple[str | None, str | None, str | None]:
    """``(event_start, event_end, event_phrase)`` as stored, each None when absent.

    A date not in the stored form (raw SQL, a legacy value) reads as None,
    matching the generated columns. Never raises.
    """
    if not isinstance(details, dict):
        return None, None, None
    start = details.get("event_start")
    end = details.get("event_end")
    phrase = details.get("event_phrase")
    if not (isinstance(start, str) and _EVENT_DATE.fullmatch(start)):
        start = None
    if not (isinstance(end, str) and _EVENT_DATE.fullmatch(end)):
        end = None
    if start is not None and end is None:
        end = start
    if start is None:
        end = None
    return start, end, phrase if isinstance(phrase, str) and phrase else None


def event_response_kwargs(details: Any) -> dict[str, str | None]:
    """The ``event_start`` / ``event_end`` / ``event_phrase`` keyword arguments of a
    write response model, read from the stored details (each None when absent)."""
    start, end, phrase = event_dates_from_details(details)
    return {"event_start": start, "event_end": end, "event_phrase": phrase}


def event_row_kwargs(details: Any) -> dict[str, str | None]:
    """The ``event_start`` / ``event_end`` keyword arguments of a read model
    (``MemoryResponse``, ``ReferenceResponse``), from the stored details."""
    start, end, _ = event_dates_from_details(details)
    return {"event_start": start, "event_end": end}


# ------------------------------------------------------------ filters, payload


def event_payload_value(day: str | None) -> str | None:
    """The search-payload form of a stored event day: the start of that day, UTC.

    ``YYYY-MM-DDT00:00:00Z`` — the 20-char form of ``observed_at``'s payload,
    so Qdrant's datetime index and LanceDB's lexical compare serve the overlap
    range the same way. None for None.
    """
    if not isinstance(day, str) or not _EVENT_DATE.fullmatch(day):
        return None
    return day + _PAYLOAD_SUFFIX


def event_payload_values(details: Any) -> tuple[str | None, str | None]:
    """``(event_start, event_end)`` in the payload form, from details; both None without a start."""
    start, end, _ = event_dates_from_details(details)
    return event_payload_value(start), event_payload_value(end)


def parse_event_bound(value: Any, name: str) -> str:
    """A filter bound (``event_after`` / ``event_before``) as a ``YYYY-MM-DD`` day.

    Accepts what ``observed_at`` accepts: an ISO 8601 extended date or datetime
    (naive = UTC). A date is the day itself. A datetime is reduced to a day so
    that the window ``[event_after, event_before)`` over time keeps its meaning
    over whole days: ``event_after`` takes its UTC day (an event ending on that
    day still overlaps), ``event_before`` takes its UTC day at midnight and the
    day after otherwise (an event starting on that day still overlaps a window
    that ends during it).

    Raises:
        EventDateValidationError: not an ISO 8601 date or datetime string.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise EventDateValidationError(f"{name} must be an ISO 8601 date or datetime string")
    try:
        canonical = canonical_observed_at(value)
        day = date.fromisoformat(canonical[:10])
        if name == "event_before" and len(canonical) > 10 and canonical[11:19] != "00:00:00":
            day += timedelta(days=1)
    except (ObservedAtValidationError, ValueError, OverflowError) as exc:
        raise EventDateValidationError(
            f"{name} must be an ISO 8601 date (2024-03-07) or datetime (2024-03-07T10:00:00Z)"
        ) from exc
    return day.isoformat()


def event_weekday(value: Any) -> str | None:
    """``Mon`` … ``Sun`` of a ``YYYY-MM-DD`` value, or None. Never raises."""
    if not isinstance(value, str) or not _EVENT_DATE.fullmatch(value):
        return None
    try:
        return WEEKDAYS[date.fromisoformat(value).weekday()]
    except ValueError:
        return None


def event_fields(start: Any, end: Any) -> dict[str, str]:
    """``event_start``, ``event_end``, ``event_weekday`` for a row; empty without a start.

    The keys are absent, never null. ``event_weekday`` is the weekday of
    ``event_start`` (the day a single-day event happened; a range's first day).
    """
    if not isinstance(start, str) or not start:
        return {}
    fields = {"event_start": start, "event_end": end if isinstance(end, str) and end else start}
    weekday = event_weekday(start)
    if weekday is not None:
        fields["event_weekday"] = weekday
    return fields


UNRESOLVED_NO_OBSERVED_AT = "no_observed_at"
UNRESOLVED_UNKNOWN_PHRASE = "unknown_phrase"


def event_write_fields(result: Any) -> dict[str, Any]:
    """The event keys of a write response (``remember``, a batch item, ``update_memory``).

    ``event_start`` / ``event_end`` / ``event_weekday`` when the stored memory
    has them; otherwise, when it carries a phrase, ``event_unresolved`` with
    the reason: ``no_observed_at`` (nothing to count from: the server never
    resolves from the write time) or ``unknown_phrase`` (outside the rule
    set). Nothing when the memory has no phrase.
    """
    fields: dict[str, Any] = event_fields(
        getattr(result, "event_start", None), getattr(result, "event_end", None)
    )
    if not fields and getattr(result, "event_phrase", None):
        fields["event_unresolved"] = (
            UNRESOLVED_UNKNOWN_PHRASE
            if getattr(result, "observed_at", None)
            else UNRESOLVED_NO_OBSERVED_AT
        )
    return fields
