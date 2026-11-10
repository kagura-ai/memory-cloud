"""Pure-logic tests for utils/event_dates.py: relative event dates resolved at write time.

Every rule of :func:`resolve_event_phrase` is pinned with a fixed anchor —
across a year boundary, on a Saturday and a Sunday, from a month without 31
days, for months that start midweek — plus the write-side contract of
:func:`normalize_event_dates` (the phrase stays verbatim; no anchor or an
unknown phrase leaves the dates absent; an explicit pair is validated) and
the response readers.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from models.memory import EVENT_END_SQL, EVENT_START_SQL, Memory
from utils.event_dates import (
    EVENT_DATE_RE,
    MAX_PHRASE_CHARS,
    EventDateValidationError,
    event_dates_from_details,
    event_fields,
    event_payload_value,
    event_payload_values,
    event_response_kwargs,
    event_row_kwargs,
    event_weekday,
    event_write_fields,
    normalize_event_dates,
    parse_event_bound,
    resolve_event_phrase,
)

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "f31_2724_event_dates.py"
)

# A Friday nine days into the year: every "last ..." crosses into 2025.
NEW_YEAR = date(2026, 1, 9)
MONDAY = date(2024, 3, 11)
SATURDAY = date(2024, 3, 9)
SUNDAY = date(2024, 3, 10)
MARCH_31 = date(2026, 3, 31)  # a Tuesday; February has 28 days


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _span(start: str, end: str | None = None) -> tuple[date, date]:
    return _d(start), _d(end or start)


class TestAnchorsAreWhatTheTestsAssume:
    def test_weekdays(self):
        assert NEW_YEAR.weekday() == 4
        assert MONDAY.weekday() == 0
        assert SATURDAY.weekday() == 5
        assert SUNDAY.weekday() == 6
        assert MARCH_31.weekday() == 1
        assert date(2026, 8, 1).weekday() == 5  # August 2026 starts on a Saturday
        assert date(2026, 10, 1).weekday() == 3  # October 2026 starts on a Thursday


class TestDays:
    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("today", "2026-01-09"),
            ("yesterday", "2026-01-08"),
            ("tomorrow", "2026-01-10"),
            ("2 days ago", "2026-01-07"),
            ("two days ago", "2026-01-07"),
            ("one day ago", "2026-01-08"),
            ("twelve days ago", "2025-12-28"),
            ("a week ago", "2026-01-02"),
            ("three weeks ago", "2025-12-19"),  # across the year boundary
            ("2 weeks ago", "2025-12-26"),
            ("a month ago", "2025-12-09"),
            ("two months ago", "2025-11-09"),
        ],
    )
    def test_single_days_from_the_new_year_anchor(self, phrase, expected):
        assert resolve_event_phrase(phrase, NEW_YEAR) == _span(expected)

    def test_months_ago_clamps_to_the_shorter_month(self):
        assert resolve_event_phrase("one month ago", MARCH_31) == _span("2026-02-28")
        assert resolve_event_phrase("two months ago", MARCH_31) == _span("2026-01-31")


class TestWeeksAndWeekends:
    @pytest.mark.parametrize(
        ("phrase", "start", "end"),
        [
            ("last week", "2024-03-04", "2024-03-10"),
            ("this week", "2024-03-11", "2024-03-17"),
            ("next week", "2024-03-18", "2024-03-24"),
            ("last weekend", "2024-03-09", "2024-03-10"),
            ("this weekend", "2024-03-16", "2024-03-17"),
            ("next weekend", "2024-03-23", "2024-03-24"),
            ("one weekend ago", "2024-03-09", "2024-03-10"),
            ("two weekends ago", "2024-03-02", "2024-03-03"),
            ("3 weekends ago", "2024-02-24", "2024-02-25"),
        ],
    )
    def test_from_a_monday(self, phrase, start, end):
        assert resolve_event_phrase(phrase, MONDAY) == _span(start, end)

    @pytest.mark.parametrize(
        ("phrase", "start", "end"),
        [
            # On a Saturday "last weekend" is the weekend before the current one.
            ("last weekend", "2024-03-02", "2024-03-03"),
            ("this weekend", "2024-03-09", "2024-03-10"),
            ("next weekend", "2024-03-16", "2024-03-17"),
            ("two weekends ago", "2024-02-24", "2024-02-25"),
            ("this week", "2024-03-04", "2024-03-10"),
            ("last week", "2024-02-26", "2024-03-03"),
        ],
    )
    def test_from_a_saturday(self, phrase, start, end):
        assert resolve_event_phrase(phrase, SATURDAY) == _span(start, end)

    @pytest.mark.parametrize(
        ("phrase", "start", "end"),
        [
            ("last weekend", "2024-03-02", "2024-03-03"),
            ("this weekend", "2024-03-09", "2024-03-10"),
            ("this week", "2024-03-04", "2024-03-10"),
            ("next week", "2024-03-11", "2024-03-17"),
            ("yesterday", "2024-03-09", "2024-03-09"),
        ],
    )
    def test_from_a_sunday(self, phrase, start, end):
        assert resolve_event_phrase(phrase, SUNDAY) == _span(start, end)

    def test_last_week_and_weekend_across_the_year_boundary(self):
        assert resolve_event_phrase("last week", NEW_YEAR) == _span("2025-12-29", "2026-01-04")
        assert resolve_event_phrase("last weekend", NEW_YEAR) == _span("2026-01-03", "2026-01-04")
        assert resolve_event_phrase("two weekends ago", NEW_YEAR) == _span(
            "2025-12-27", "2025-12-28"
        )


class TestMonthsAndYears:
    def test_last_month_from_the_new_year_is_december_of_the_year_before(self):
        assert resolve_event_phrase("last month", NEW_YEAR) == _span("2025-12-01", "2025-12-31")

    def test_last_month_from_march_31_is_the_whole_of_february(self):
        assert resolve_event_phrase("last month", MARCH_31) == _span("2026-02-01", "2026-02-28")

    def test_this_and_next_month(self):
        assert resolve_event_phrase("this month", MARCH_31) == _span("2026-03-01", "2026-03-31")
        assert resolve_event_phrase("next month", MARCH_31) == _span("2026-04-01", "2026-04-30")
        assert resolve_event_phrase("next month", NEW_YEAR) == _span("2026-02-01", "2026-02-28")

    def test_years(self):
        assert resolve_event_phrase("last year", NEW_YEAR) == _span("2025-01-01", "2025-12-31")
        assert resolve_event_phrase("this year", NEW_YEAR) == _span("2026-01-01", "2026-12-31")
        assert resolve_event_phrase("next year", NEW_YEAR) == _span("2027-01-01", "2027-12-31")

    @pytest.mark.parametrize(
        ("phrase", "anchor", "start", "end"),
        [
            # A month name is the most recent such month that started on or
            # before the anchor: December from 2026-01-09 is December 2025.
            ("December", NEW_YEAR, "2025-12-01", "2025-12-31"),
            ("in January", NEW_YEAR, "2026-01-01", "2026-01-31"),
            ("June", date(2026, 6, 15), "2026-06-01", "2026-06-30"),
            ("july", date(2026, 6, 15), "2025-07-01", "2025-07-31"),
            ("September", date(2026, 9, 1), "2026-09-01", "2026-09-30"),
            ("Sept", date(2026, 9, 1), "2026-09-01", "2026-09-30"),
            ("aug", date(2026, 9, 1), "2026-08-01", "2026-08-31"),
            ("February", date(2024, 3, 1), "2024-02-01", "2024-02-29"),  # a leap year
        ],
    )
    def test_a_month_name_is_the_whole_month(self, phrase, anchor, start, end):
        assert resolve_event_phrase(phrase, anchor) == _span(start, end)


class TestWeekOfMonth:
    """The Mon–Sun week holding the month's first / last Thursday."""

    @pytest.mark.parametrize(
        ("phrase", "anchor", "start", "end"),
        [
            # August 2026 starts on a Saturday: its first week starts on Mon 3rd,
            # and the 31st (a Monday) already belongs to September's first week.
            ("the first week of August", date(2026, 9, 1), "2026-08-03", "2026-08-09"),
            ("the last week of August", date(2026, 9, 1), "2026-08-24", "2026-08-30"),
            # October 2026 starts on a Thursday: its first week begins in September.
            ("first week of October", date(2026, 10, 15), "2026-09-28", "2026-10-04"),
            ("last week of September", date(2026, 10, 15), "2026-09-21", "2026-09-27"),
            # December 2025 starts on a Monday.
            ("first week of December", NEW_YEAR, "2025-12-01", "2025-12-07"),
            ("last week of December", NEW_YEAR, "2025-12-22", "2025-12-28"),
            # January 2026 starts on a Thursday: its first week begins in 2025.
            ("first week of January", NEW_YEAR, "2025-12-29", "2026-01-04"),
        ],
    )
    def test_first_and_last_week(self, phrase, anchor, start, end):
        assert resolve_event_phrase(phrase, anchor) == _span(start, end)

    def test_an_unknown_month_is_unresolved(self):
        assert resolve_event_phrase("first week of Brumaire", NEW_YEAR) is None


class TestWeekdays:
    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("Thursday", "2026-01-08"),
            ("thu", "2026-01-08"),
            ("Friday", "2026-01-02"),  # strictly before the anchor: a week back
            ("Saturday", "2026-01-03"),
            ("last Saturday", "2026-01-03"),
            ("on Monday", "2026-01-05"),
            ("Sunday", "2026-01-04"),
        ],
    )
    def test_the_most_recent_one_strictly_before_the_anchor(self, phrase, expected):
        assert resolve_event_phrase(phrase, NEW_YEAR) == _span(expected)

    def test_from_that_weekday_itself(self):
        assert resolve_event_phrase("Saturday", SATURDAY) == _span("2024-03-02")
        assert resolve_event_phrase("Sunday", SUNDAY) == _span("2024-03-03")


class TestTolerance:
    @pytest.mark.parametrize(
        "phrase",
        [
            "last weekend",
            "Last Weekend",
            "LAST WEEKEND.",
            "  last   weekend  ",
            "on the last weekend",
        ],
    )
    def test_case_whitespace_and_leading_words(self, phrase):
        assert resolve_event_phrase(phrase, MONDAY) == _span("2024-03-09", "2024-03-10")

    def test_in_and_the(self):
        assert resolve_event_phrase("in the last week of August", date(2026, 9, 1)) == _span(
            "2026-08-24", "2026-08-30"
        )
        assert resolve_event_phrase("Today.", MONDAY) == _span("2024-03-11")


class TestUnresolved:
    @pytest.mark.parametrize(
        "phrase",
        [
            "",
            "   ",
            "the",
            "soon",
            "a few days ago",
            "a couple of weeks ago",
            "last December",
            "in two days",
            "0 days ago",
            "zero days ago",
            "thirteen days ago",  # words stop at twelve; digits go on
            "an hour ago",
            "2024-03-07",
            "last weekend of August",
            "first weekend",
        ],
    )
    def test_anything_outside_the_rules_is_none(self, phrase):
        assert resolve_event_phrase(phrase, MONDAY) is None

    def test_no_clock_is_read(self):
        """The same phrase and anchor always give the same days."""
        assert resolve_event_phrase("yesterday", MONDAY) == resolve_event_phrase(
            "yesterday", MONDAY
        )

    def test_non_string_or_non_date_inputs_are_none(self):
        assert resolve_event_phrase(None, MONDAY) is None  # type: ignore[arg-type]
        assert resolve_event_phrase("today", "2024-03-11") is None  # type: ignore[arg-type]


class TestPartsOfTheDay:
    """#2751: a part of the material's day is that day; "last night" the day before."""

    @pytest.mark.parametrize(
        "phrase", ["tonight", "Tonight.", "this morning", "this afternoon", "this evening"]
    )
    def test_a_part_of_the_day_is_the_anchor(self, phrase):
        assert resolve_event_phrase(phrase, SATURDAY) == _span("2024-03-09")

    def test_last_night_is_the_day_before(self):
        assert resolve_event_phrase("last night", SATURDAY) == _span("2024-03-08")
        # Across a month end in a leap year.
        assert resolve_event_phrase("last night", date(2024, 3, 1)) == _span("2024-02-29")
        assert resolve_event_phrase("Last Night", NEW_YEAR) == _span("2026-01-08")


class TestTwoDaysAway:
    @pytest.mark.parametrize(
        ("phrase", "anchor", "expected"),
        [
            ("day after tomorrow", SATURDAY, "2024-03-11"),
            ("the day after tomorrow", SATURDAY, "2024-03-11"),
            ("day before yesterday", SATURDAY, "2024-03-07"),
            ("the day before yesterday", SATURDAY, "2024-03-07"),
            # Across a month end (2024 is a leap year) and a year end.
            ("the day after tomorrow", date(2024, 2, 28), "2024-03-01"),
            ("the day before yesterday", date(2024, 3, 1), "2024-02-28"),
            ("the day after tomorrow", date(2025, 12, 31), "2026-01-02"),
            ("the day before yesterday", NEW_YEAR, "2026-01-07"),
        ],
    )
    def test_plus_or_minus_two_days(self, phrase, anchor, expected):
        assert resolve_event_phrase(phrase, anchor) == _span(expected)


class TestThisAndNextWeekday:
    """#2751: ``this <weekday>`` is in the anchor's Mon–Sun week, ``next <weekday>``
    in the Mon–Sun week after it (reading B)."""

    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("this Sunday", "2024-03-10"),
            ("next Sunday", "2024-03-17"),
            ("next Monday", "2024-03-11"),
            ("this Monday", "2024-03-04"),  # before the anchor, same week
            ("this Saturday", "2024-03-09"),  # the anchor itself
            ("next Saturday", "2024-03-16"),
            ("this Friday", "2024-03-08"),
            ("next fri", "2024-03-15"),
            ("on this sun", "2024-03-10"),
        ],
    )
    def test_from_a_saturday(self, phrase, expected):
        assert resolve_event_phrase(phrase, SATURDAY) == _span(expected)

    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("this Monday", "2024-03-11"),  # the anchor itself
            ("next Monday", "2024-03-18"),
            ("this Tuesday", "2024-03-12"),
            ("next Tuesday", "2024-03-19"),
            ("this Sunday", "2024-03-17"),
            ("next Sunday", "2024-03-24"),
        ],
    )
    def test_from_a_monday(self, phrase, expected):
        assert resolve_event_phrase(phrase, MONDAY) == _span(expected)

    @pytest.mark.parametrize(
        ("phrase", "anchor", "expected"),
        [
            # Fri 2024-05-31: the week runs Mon 27 May - Sun 2 June.
            ("this Monday", date(2024, 5, 31), "2024-05-27"),
            ("this Sunday", date(2024, 5, 31), "2024-06-02"),
            ("next Monday", date(2024, 5, 31), "2024-06-03"),
            ("next Wednesday", date(2024, 5, 31), "2024-06-05"),
            # Wed 2025-12-31: the week runs Mon 29 Dec - Sun 4 January.
            ("this Monday", date(2025, 12, 31), "2025-12-29"),
            ("this Sunday", date(2025, 12, 31), "2026-01-04"),
            ("next Tuesday", date(2025, 12, 31), "2026-01-06"),
        ],
    )
    def test_across_a_month_and_a_year_end(self, phrase, anchor, expected):
        assert resolve_event_phrase(phrase, anchor) == _span(expected)

    @pytest.mark.parametrize(
        ("phrase", "start", "end"),
        [
            ("this week", "2024-03-04", "2024-03-10"),
            ("next week", "2024-03-11", "2024-03-17"),
            ("this weekend", "2024-03-09", "2024-03-10"),
            ("next weekend", "2024-03-16", "2024-03-17"),
            ("this month", "2024-03-01", "2024-03-31"),
            ("next month", "2024-04-01", "2024-04-30"),
            ("this year", "2024-01-01", "2024-12-31"),
            ("next year", "2025-01-01", "2025-12-31"),
        ],
    )
    def test_the_periods_are_not_shadowed(self, phrase, start, end):
        assert resolve_event_phrase(phrase, SATURDAY) == _span(start, end)

    @pytest.mark.parametrize("phrase", ["this March", "next June", "this day", "next time"])
    def test_a_word_that_is_no_weekday_is_unresolved(self, phrase):
        assert resolve_event_phrase(phrase, SATURDAY) is None


class TestYearsAgo:
    """#2751: the same calendar day N years back, clamped as ``N months ago``."""

    @pytest.mark.parametrize(
        ("phrase", "anchor", "expected"),
        [
            ("three years ago", NEW_YEAR, "2023-01-09"),
            ("a year ago", NEW_YEAR, "2025-01-09"),
            ("one year ago", SATURDAY, "2023-03-09"),
            ("2 years ago", SATURDAY, "2022-03-09"),
            ("twelve years ago", SATURDAY, "2012-03-09"),
            ("100 years ago", SATURDAY, "1924-03-09"),
            # 29 February: clamped in a non-leap year, kept in a leap year.
            ("a year ago", date(2024, 2, 29), "2023-02-28"),
            ("four years ago", date(2024, 2, 29), "2020-02-29"),
            ("3 years ago", date(2024, 2, 29), "2021-02-28"),
        ],
    )
    def test_the_same_calendar_day(self, phrase, anchor, expected):
        assert resolve_event_phrase(phrase, anchor) == _span(expected)

    @pytest.mark.parametrize(
        ("phrase", "anchor"),
        [
            ("5 years ago", date(5, 6, 1)),
            ("a year ago", date(1, 1, 1)),
            ("1000 years ago", date(500, 1, 1)),
        ],
    )
    def test_a_year_below_one_is_unresolved_not_an_error(self, phrase, anchor):
        assert resolve_event_phrase(phrase, anchor) is None

    def test_a_year_below_one_leaves_the_write_unresolved(self):
        details = normalize_event_dates({"observed_at": "0001-06-01", "event_phrase": "a year ago"})
        assert details == {"observed_at": "0001-06-01", "event_phrase": "a year ago"}

    @pytest.mark.parametrize("phrase", ["thirteen years ago", "0 years ago", "1001 years ago"])
    def test_counts_outside_the_words_or_the_cap_are_unresolved(self, phrase):
        assert resolve_event_phrase(phrase, SATURDAY) is None


class TestUnresolvedOnPurpose:
    """#2751: phrases that name no single day or range, a season, a phrase counted
    from another event, and the open items the issue leaves for later."""

    @pytest.mark.parametrize(
        "phrase",
        [
            "a few years back",
            "the other day",
            "lately",
            "a while back",
            "a few weeks ago",
            "last summer",
            "two weekends later",
            # Open, lower priority in #2751: ranges ending on the anchor, "around".
            "the past week",
            "the last 3 days",
            "around yesterday",
            "about two weeks ago",
        ],
    )
    def test_they_stay_unknown(self, phrase):
        assert resolve_event_phrase(phrase, SATURDAY) is None
        result = SimpleNamespace(
            event_start=None, event_end=None, event_phrase=phrase, observed_at="2024-03-09"
        )
        assert event_write_fields(result) == {"event_unresolved": "unknown_phrase"}


FRIDAY = date(2024, 3, 8)


class TestEventDirection:
    """#2752: ``event_direction="future"`` turns a BARE weekday into the first such
    day on or after the anchor; ``"past"`` or no direction keeps the present rule;
    every other phrase ignores it."""

    @pytest.mark.parametrize(
        ("anchor", "future", "past"),
        [
            (FRIDAY, "2024-03-10", "2024-03-03"),  # before the named weekday
            (SUNDAY, "2024-03-10", "2024-03-03"),  # on it: the anchor itself
            (MONDAY, "2024-03-17", "2024-03-10"),  # after it
        ],
    )
    @pytest.mark.parametrize("phrase", ["Sunday", "on Sunday", "sun"])
    def test_a_bare_weekday(self, phrase, anchor, future, past):
        assert resolve_event_phrase(phrase, anchor, "future") == _span(future)
        assert resolve_event_phrase(phrase, anchor, "past") == _span(past)
        assert resolve_event_phrase(phrase, anchor) == _span(past)
        assert resolve_event_phrase(phrase, anchor, None) == _span(past)

    def test_the_issue_examples(self):
        assert resolve_event_phrase("on Sunday", SATURDAY, "future") == _span("2024-03-10")
        assert resolve_event_phrase("Saturday", SATURDAY, "future") == _span("2024-03-09")
        assert resolve_event_phrase("Saturday", SATURDAY) == _span("2024-03-02")

    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("last Sunday", ("2024-03-03",)),
            ("this Sunday", ("2024-03-10",)),
            ("next Sunday", ("2024-03-17",)),
            ("yesterday", ("2024-03-08",)),
            ("last week", ("2024-02-26", "2024-03-03")),
            ("two weeks ago", ("2024-02-24",)),
            ("June", ("2023-06-01", "2023-06-30")),
        ],
    )
    @pytest.mark.parametrize("direction", ["past", "future", None])
    def test_every_other_phrase_ignores_it(self, phrase, expected, direction):
        assert resolve_event_phrase(phrase, SATURDAY, direction) == _span(*expected)

    def test_the_write_resolves_with_it_and_stores_it(self):
        details = normalize_event_dates(
            {"observed_at": "2024-03-09", "event_phrase": "on Sunday", "event_direction": "future"}
        )
        assert details == {
            "observed_at": "2024-03-09",
            "event_phrase": "on Sunday",
            "event_direction": "future",
            "event_start": "2024-03-10",
            "event_end": "2024-03-10",
        }

    @pytest.mark.parametrize(("given", "stored"), [(" Future ", "future"), ("PAST", "past")])
    def test_it_is_lower_cased_and_stripped(self, given, stored):
        details = normalize_event_dates(
            {"observed_at": "2024-03-09", "event_phrase": "Sunday", "event_direction": given}
        )
        assert details["event_direction"] == stored

    def test_it_alone_is_kept_and_null_removes_it(self):
        assert normalize_event_dates({"event_direction": "past"}) == {"event_direction": "past"}
        assert normalize_event_dates({"event_direction": None, "k": 1}) == {"k": 1}

    @pytest.mark.parametrize("value", ["soon", "", "  ", "forward", 1, True, ["future"]])
    def test_any_other_value_is_refused(self, value):
        with pytest.raises(
            EventDateValidationError, match='event_direction must be "past" or "future"'
        ):
            normalize_event_dates(
                {"observed_at": "2024-03-09", "event_phrase": "Sunday", "event_direction": value}
            )


# --------------------------------------------------------------- write path


class TestNormalize:
    def test_none_and_untouched_pass_through(self):
        assert normalize_event_dates(None) is None
        details = {"observed_at": "2024-03-11", "k": 1}
        assert normalize_event_dates(details) is details
        assert details == {"observed_at": "2024-03-11", "k": 1}

    def test_the_phrase_is_kept_verbatim_and_resolved_from_observed_at(self):
        details = normalize_event_dates(
            {"observed_at": "2024-03-11", "event_phrase": "Last weekend"}
        )
        assert details == {
            "observed_at": "2024-03-11",
            "event_phrase": "Last weekend",
            "event_start": "2024-03-09",
            "event_end": "2024-03-10",
        }

    def test_a_single_day_has_equal_ends(self):
        details = normalize_event_dates({"observed_at": "2024-03-11", "event_phrase": "yesterday"})
        assert (details["event_start"], details["event_end"]) == ("2024-03-10", "2024-03-10")

    def test_the_anchor_is_the_stored_utc_day_of_a_datetime(self):
        details = normalize_event_dates(
            {"observed_at": "2024-03-11T23:30:00Z", "event_phrase": "yesterday"}
        )
        assert details["event_start"] == "2024-03-10"

    def test_no_observed_at_leaves_the_dates_absent(self):
        """Never resolved from the write time: the material may be years old."""
        details = normalize_event_dates({"event_phrase": "yesterday", "k": 1})
        assert details == {"event_phrase": "yesterday", "k": 1}

    def test_a_legacy_observed_at_is_no_anchor(self):
        details = normalize_event_dates({"observed_at": "March 7", "event_phrase": "yesterday"})
        assert "event_start" not in details and details["event_phrase"] == "yesterday"

    def test_an_unknown_phrase_keeps_an_explicit_pair_the_caller_supplied(self):
        """Dates the writer knew are not lost to a phrase the server cannot count."""
        details = normalize_event_dates(
            {
                "observed_at": "2024-03-11",
                "event_phrase": "a few days ago",
                "event_start": "2024-03-01",
                "event_end": "2024-03-02",
            }
        )
        assert details == {
            "observed_at": "2024-03-11",
            "event_phrase": "a few days ago",
            "event_start": "2024-03-01",
            "event_end": "2024-03-02",
        }

    def test_an_unknown_phrase_without_a_pair_leaves_the_dates_absent(self):
        details = normalize_event_dates(
            {"observed_at": "2024-03-11", "event_phrase": "a few days ago"}
        )
        assert details == {"observed_at": "2024-03-11", "event_phrase": "a few days ago"}
        # A lone end never survives (there is no start to pair it with).
        details = normalize_event_dates(
            {
                "observed_at": "2024-03-11",
                "event_phrase": "a few days ago",
                "event_end": "2024-03-02",
            }
        )
        assert details == {"observed_at": "2024-03-11", "event_phrase": "a few days ago"}

    def test_no_observed_at_keeps_an_explicit_pair(self):
        details = normalize_event_dates({"event_phrase": "yesterday", "event_start": "2024-03-10"})
        assert details == {
            "event_phrase": "yesterday",
            "event_start": "2024-03-10",
            "event_end": "2024-03-10",
        }

    @pytest.mark.parametrize(
        ("anchor", "phrase"),
        [("0001-01-01", "yesterday"), ("9999-12-31", "tomorrow"), ("0001-01-03", "two weeks ago")],
    )
    def test_arithmetic_that_leaves_the_calendar_is_unresolved_not_an_error(self, anchor, phrase):
        details = normalize_event_dates({"observed_at": anchor, "event_phrase": phrase})
        assert details == {"observed_at": anchor, "event_phrase": phrase}

    def test_a_phrase_overrides_a_client_pair(self):
        details = normalize_event_dates(
            {
                "observed_at": "2024-03-11",
                "event_phrase": "last weekend",
                "event_start": "2000-01-01",
            }
        )
        assert (details["event_start"], details["event_end"]) == ("2024-03-09", "2024-03-10")

    def test_null_removes_the_keys(self):
        assert normalize_event_dates({"event_phrase": None, "k": 1}) == {"k": 1}
        assert normalize_event_dates({"event_start": None, "event_end": None}) == {}

    @pytest.mark.parametrize(
        "phrase", ["", "   ", 5, True, ["yesterday"], "x" * (MAX_PHRASE_CHARS + 1)]
    )
    def test_a_bad_phrase_is_refused(self, phrase):
        with pytest.raises(EventDateValidationError, match="event_phrase"):
            normalize_event_dates({"observed_at": "2024-03-11", "event_phrase": phrase})

    def test_an_explicit_pair_without_a_phrase_is_kept(self):
        details = normalize_event_dates({"event_start": "2024-03-09", "event_end": "2024-03-10"})
        assert details == {"event_start": "2024-03-09", "event_end": "2024-03-10"}

    def test_an_explicit_start_alone_is_a_single_day(self):
        assert normalize_event_dates({"event_start": " 2024-03-09 "}) == {
            "event_start": "2024-03-09",
            "event_end": "2024-03-09",
        }

    @pytest.mark.parametrize(
        "details",
        [
            {"event_end": "2024-03-10"},
            {"event_start": None, "event_end": "2024-03-10"},
            {"event_start": "2024-03-10", "event_end": "2024-03-09"},
            {"event_start": "2024-3-9"},
            {"event_start": "2024-02-30"},
            {"event_start": "2024-03-09T00:00:00Z"},
            {"event_start": 20240309},
            {"event_start": True},
            {"event_start": "2024-03-09", "event_end": "soon"},
        ],
    )
    def test_a_bad_pair_is_refused(self, details):
        with pytest.raises(EventDateValidationError):
            normalize_event_dates(details)

    def test_it_is_a_value_error(self):
        assert issubclass(EventDateValidationError, ValueError)


# ------------------------------------------------------------------ readers


class TestReaders:
    def test_from_details(self):
        assert event_dates_from_details(None) == (None, None, None)
        assert event_dates_from_details("x") == (None, None, None)
        assert event_dates_from_details({}) == (None, None, None)
        assert event_dates_from_details({"event_start": "2024-03-09"}) == (
            "2024-03-09",
            "2024-03-09",
            None,
        )
        assert event_dates_from_details(
            {"event_start": "2024-03-09", "event_end": "2024-03-10", "event_phrase": "last weekend"}
        ) == ("2024-03-09", "2024-03-10", "last weekend")
        # An end without a start, a legacy form or an empty phrase read as absent.
        assert event_dates_from_details({"event_end": "2024-03-10"}) == (None, None, None)
        assert event_dates_from_details({"event_start": "March 9"}) == (None, None, None)
        assert event_dates_from_details({"event_phrase": ""}) == (None, None, None)

    def test_response_kwargs(self):
        assert event_response_kwargs({"event_phrase": "soon"}) == {
            "event_start": None,
            "event_end": None,
            "event_phrase": "soon",
        }
        assert event_row_kwargs({"event_start": "2024-03-09", "event_phrase": "x"}) == {
            "event_start": "2024-03-09",
            "event_end": "2024-03-09",
        }

    def test_weekday(self):
        assert event_weekday("2024-03-09") == "Sat"
        assert event_weekday("2024-03-11") == "Mon"
        for value in (None, "", "2024-02-30", "2024-03-09T00:00:00Z", 5):
            assert event_weekday(value) is None

    def test_fields_follow_the_start(self):
        fields = event_fields("2024-03-09", "2024-03-10")
        assert fields == {
            "event_start": "2024-03-09",
            "event_end": "2024-03-10",
            "event_weekday": "Sat",
        }
        assert list(fields) == ["event_start", "event_end", "event_weekday"]
        assert event_fields("2024-03-09", None)["event_end"] == "2024-03-09"
        assert event_fields(None, "2024-03-10") == {}
        assert event_fields("", "") == {}
        assert event_fields("2024-02-30", "2024-02-30") == {
            "event_start": "2024-02-30",
            "event_end": "2024-02-30",
        }

    def test_write_fields(self):
        resolved = SimpleNamespace(
            observed_at="2024-03-11",
            event_start="2024-03-09",
            event_end="2024-03-10",
            event_phrase="last weekend",
        )
        assert event_write_fields(resolved) == {
            "event_start": "2024-03-09",
            "event_end": "2024-03-10",
            "event_weekday": "Sat",
        }
        unknown = SimpleNamespace(
            observed_at="2024-03-11", event_start=None, event_end=None, event_phrase="soon"
        )
        assert event_write_fields(unknown) == {"event_unresolved": "unknown_phrase"}
        no_anchor = SimpleNamespace(
            observed_at=None, event_start=None, event_end=None, event_phrase="yesterday"
        )
        assert event_write_fields(no_anchor) == {"event_unresolved": "no_observed_at"}
        bare = SimpleNamespace(
            observed_at=None, event_start=None, event_end=None, event_phrase=None
        )
        assert event_write_fields(bare) == {}
        assert event_write_fields(object()) == {}


# ---------------------------------------------------------- filters, payload


class TestBounds:
    def test_a_date_is_the_day(self):
        assert parse_event_bound("2024-03-09", "event_after") == "2024-03-09"
        assert parse_event_bound("2024-03-09", "event_before") == "2024-03-09"

    def test_a_datetime_is_reduced_to_its_utc_day(self):
        # after: the day itself (an event ending that day still overlaps).
        assert parse_event_bound("2024-03-09T12:00:00Z", "event_after") == "2024-03-09"
        assert parse_event_bound("2024-03-10T08:00:00+09:00", "event_after") == "2024-03-09"
        # before: midnight is the day; any later time means the day after
        # (an event starting that day still overlaps a window ending during it).
        assert parse_event_bound("2024-03-09T00:00:00Z", "event_before") == "2024-03-09"
        assert parse_event_bound("2024-03-09T00:00:01Z", "event_before") == "2024-03-10"
        assert parse_event_bound("2024-03-10T09:00:00+09:00", "event_before") == "2024-03-10"

    @pytest.mark.parametrize("value", [None, 1, True, "soon", "20240309", ["2024-03-09"]])
    def test_a_bad_bound_names_the_filter(self, value):
        with pytest.raises(EventDateValidationError, match="event_after"):
            parse_event_bound(value, "event_after")


class TestPayload:
    def test_the_payload_form_is_the_days_utc_midnight(self):
        assert event_payload_value("2024-03-09") == "2024-03-09T00:00:00Z"
        for value in (None, "", "March 9", "2024-03-09T00:00:00Z"):
            assert event_payload_value(value) is None

    def test_from_details(self):
        assert event_payload_values({"event_start": "2024-03-09", "event_end": "2024-03-10"}) == (
            "2024-03-09T00:00:00Z",
            "2024-03-10T00:00:00Z",
        )
        assert event_payload_values({"event_start": "2024-03-09"}) == (
            "2024-03-09T00:00:00Z",
            "2024-03-09T00:00:00Z",
        )
        assert event_payload_values({"event_phrase": "soon"}) == (None, None)
        assert event_payload_values(None) == (None, None)


class TestSqlParity:
    def test_the_regex_is_the_columns(self):
        assert f"'{EVENT_DATE_RE}'" in EVENT_START_SQL
        assert f"'{EVENT_DATE_RE}'" in EVENT_END_SQL

    def test_the_model_columns_use_the_expressions(self):
        for name, sql in (("event_start", EVENT_START_SQL), ("event_end", EVENT_END_SQL)):
            computed = Memory.__table__.c[name].computed
            assert computed is not None
            assert str(computed.sqltext) == sql
            assert computed.persisted is True

    def test_the_migration_expressions_are_byte_identical(self):
        spec = importlib.util.spec_from_file_location("f31_2724_event_dates", _MIGRATION)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.EVENT_START_SQL == EVENT_START_SQL
        assert module.EVENT_END_SQL == EVENT_END_SQL
        assert "WHERE event_start IS NOT NULL AND deleted_at IS NULL" in module._INDEX_DDL

    def test_the_model_index_predicate_matches_the_migration(self):
        index = next(i for i in Memory.__table__.indexes if i.name == "idx_memories_event_start")
        assert [c.name for c in index.columns] == ["context_id", "event_start"]
        assert (
            str(index.dialect_options["postgresql"]["where"])
            == "event_start IS NOT NULL AND deleted_at IS NULL"
        )
