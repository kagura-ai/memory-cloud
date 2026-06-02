import pytest

from utils.time_trigger import (
    TriggerValidationError,
    format_trigger_label,
    normalize_trigger,
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
        ({"year": 2026}, "2026年ごろ"),
        ({"year": 2026, "month": 7}, "2026年7月ごろ"),
        ({"year": 2026, "month": 7, "day": 15}, "2026-07-15"),
        ({"year": 2026, "month": 7, "day": 15, "hour": 10, "minute": 30}, "2026-07-15 10:30"),
    ],
)
def test_format_trigger_label(trigger, expected):
    assert format_trigger_label(normalize_trigger(trigger)) == expected
