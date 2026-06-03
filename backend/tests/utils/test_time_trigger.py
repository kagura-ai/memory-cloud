import pytest

from utils.time_trigger import (
    TriggerValidationError,
    format_trigger_label,
    normalize_trigger,
    parse_query_bound,
)


def test_year_only_window_spans_whole_year():
    out = normalize_trigger({"year": 2026})
    assert out["from"] == "2026-01-01T00:00:00"
    assert out["until"] == "2026-12-31T23:59:59"


def test_month_only_window_spans_whole_month():
    out = normalize_trigger({"year": 2026, "month": 7})
    assert out["from"] == "2026-07-01T00:00:00"
    assert out["until"] == "2026-07-31T23:59:59"


def test_february_leap_year_last_day_is_29():
    out = normalize_trigger({"year": 2028, "month": 2})
    assert out["until"] == "2028-02-29T23:59:59"


def test_february_non_leap_year_last_day_is_28():
    out = normalize_trigger({"year": 2026, "month": 2})
    assert out["until"] == "2026-02-28T23:59:59"


def test_full_day_window_spans_whole_day():
    out = normalize_trigger({"year": 2026, "month": 7, "day": 15})
    assert out["from"] == "2026-07-15T00:00:00"
    assert out["until"] == "2026-07-15T23:59:59"


def test_full_datetime_window_is_one_minute():
    out = normalize_trigger({"year": 2026, "month": 7, "day": 15, "hour": 10, "minute": 30})
    assert out["from"] == "2026-07-15T10:30:00"
    assert out["until"] == "2026-07-15T10:30:59"


def test_components_and_text_are_echoed():
    out = normalize_trigger({"year": 2026, "month": 7, "text": "来年度の運動会"})
    assert out["year"] == 2026 and out["month"] == 7
    assert out["day"] is None and out["hour"] is None and out["minute"] is None
    assert out["text"] == "来年度の運動会"


def test_missing_year_raises():
    with pytest.raises(TriggerValidationError):
        normalize_trigger({"month": 7})


def test_day_without_month_raises():
    with pytest.raises(TriggerValidationError):
        normalize_trigger({"year": 2026, "day": 15})


def test_impossible_day_raises():
    with pytest.raises(TriggerValidationError):
        normalize_trigger({"year": 2026, "month": 2, "day": 30})


def test_month_out_of_range_raises():
    with pytest.raises(TriggerValidationError):
        normalize_trigger({"year": 2026, "month": 13})


def test_non_dict_raises():
    with pytest.raises(TriggerValidationError):
        normalize_trigger("2026-07")


@pytest.mark.parametrize(
    "trigger,expected",
    [
        ({"year": 2026}, "2026 (year)"),
        ({"year": 2026, "month": 7}, "2026-07 (month)"),
        ({"year": 2026, "month": 7, "day": 15}, "2026-07-15"),
        # hour-precision keeps the "(hour)" marker so it is distinguishable from
        # a minute-precision trigger at :00 (the two have different windows).
        ({"year": 2026, "month": 7, "day": 15, "hour": 10}, "2026-07-15 10:00 (hour)"),
        ({"year": 2026, "month": 7, "day": 15, "hour": 10, "minute": 0}, "2026-07-15 10:00"),
        ({"year": 2026, "month": 7, "day": 15, "hour": 10, "minute": 30}, "2026-07-15 10:30"),
    ],
)
def test_format_trigger_label(trigger, expected):
    assert format_trigger_label(normalize_trigger(trigger)) == expected


def test_year_9999_december_does_not_overflow():
    # _days_in_month must not compute datetime(year+1, ...) which would overflow
    # for year 9999; calendar.monthrange handles it.
    out = normalize_trigger({"year": 9999, "month": 12})
    assert out["from"] == "9999-12-01T00:00:00"
    assert out["until"] == "9999-12-31T23:59:59"


@pytest.mark.parametrize("field", ["month", "day", "hour", "minute"])
def test_bool_component_rejected(field):
    # bool is an int subclass; without an explicit guard True would be accepted
    # as 1 and stored as a JSON boolean.
    base = {"year": 2026, "month": 7, "day": 15, "hour": 10}
    base[field] = True
    with pytest.raises(TriggerValidationError):
        normalize_trigger(base)


@pytest.mark.parametrize("field", ["year", "month", "day", "hour", "minute"])
def test_float_component_rejected(field):
    base = {"year": 2026, "month": 7, "day": 15, "hour": 10, "minute": 30}
    base[field] = float(base[field])
    with pytest.raises(TriggerValidationError):
        normalize_trigger(base)


def test_parse_query_bound_none_returns_none():
    assert parse_query_bound(None) is None


def test_parse_query_bound_now_resolves_to_iso():
    out = parse_query_bound("now")
    # Fixed-width naive ISO, no Z suffix.
    assert len(out) == 19
    assert out[4] == "-" and out[10] == "T"


def test_parse_query_bound_passes_through_valid_iso():
    assert parse_query_bound("2026-07-01T00:00:00") == "2026-07-01T00:00:00"


def test_parse_query_bound_date_only_normalizes_to_midnight():
    assert parse_query_bound("2026-07-01") == "2026-07-01T00:00:00"


def test_parse_query_bound_aware_converts_to_utc_naive():
    # 2026-07-01T09:00:00+09:00 == 2026-07-01T00:00:00 UTC
    assert parse_query_bound("2026-07-01T09:00:00+09:00") == "2026-07-01T00:00:00"


@pytest.mark.parametrize("bad", ["2026-7-1T00:00:00", "", "next week", "2026/07/01"])
def test_parse_query_bound_rejects_non_iso(bad):
    with pytest.raises(TriggerValidationError):
        parse_query_bound(bad)
