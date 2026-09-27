"""Tests for the shared MCP response budget helpers (#1743)."""

import pytest

from utils.response_budget import (
    DEFAULT_MAX_CHARS,
    MAX_CHARS_LIMIT,
    MIN_MAX_CHARS,
    BudgetArgumentError,
    drop_key,
    fit_items,
    json_chars,
    parse_limit,
    parse_max_chars,
    parse_offset_cursor,
)


def test_json_chars_counts_the_compact_utf8_form():
    assert json_chars({"a": "日本"}) == len('{"a":"日本"}')


def test_max_chars_defaults_and_bounds():
    assert parse_max_chars(None) == DEFAULT_MAX_CHARS
    assert parse_max_chars(MIN_MAX_CHARS) == MIN_MAX_CHARS
    assert parse_max_chars(MAX_CHARS_LIMIT) == MAX_CHARS_LIMIT
    for bad in (MIN_MAX_CHARS - 1, MAX_CHARS_LIMIT + 1, True, "20000", 1.5):
        with pytest.raises(BudgetArgumentError):
            parse_max_chars(bad)


def test_limit_clamps_integers_and_refuses_the_rest():
    assert parse_limit(None, default=50, maximum=200) == 50
    assert parse_limit(0, default=50, maximum=200) == 1
    assert parse_limit(-3, default=50, maximum=200) == 1
    assert parse_limit(500, default=50, maximum=200) == 200
    for bad in ("10", True, 2.0):
        with pytest.raises(BudgetArgumentError):
            parse_limit(bad, default=50, maximum=200)


def test_offset_cursor_round_trips_and_refuses_garbage():
    assert parse_offset_cursor(None) == 0
    assert parse_offset_cursor("") == 0
    assert parse_offset_cursor("150") == 150
    for bad in ("-1", "abc", 5, "1.5"):
        with pytest.raises(BudgetArgumentError):
            parse_offset_cursor(bad)


def test_fit_items_returns_the_prefix_that_fits():
    items = [{"v": "x" * 10}] * 5  # 18 chars each + 1 separator
    assert fit_items(items, 19 * 5) == 5
    assert fit_items(items, 19 * 3 + 5) == 3
    assert fit_items(items, 0) == 0
    assert fit_items([], 0) == 0


def test_drop_key_copies_without_the_key():
    items = [{"a": 1, "b": 2}]
    assert drop_key(items, "b") == [{"a": 1}]
    assert items == [{"a": 1, "b": 2}]


def test_omit_field_to_fit_cuts_from_the_end():
    from utils.response_budget import omit_field_to_fit

    items = [{"id": i, "details": {"x": "y" * 100}} for i in range(5)]
    out = omit_field_to_fit(items, "details", 400)
    assert json_chars(out) <= 400
    assert "details" in out[0]
    assert out[-1]["details_omitted"] is True
    assert out[-1]["details_total_chars"] == json_chars(items[-1]["details"])
    assert len(out) == 5
    # Under the budget nothing changes.
    assert omit_field_to_fit(items, "details", 10_000) == items


def test_fit_lanes_keeps_everything_that_fits():
    from utils.response_budget import fit_lanes

    lanes = [[{"a": 1, "cs": "x"}], [{"b": 2, "cs": "y"}]]
    out, cut, dropped = fit_lanes(lanes, 10_000, droppable="cs")
    assert out == lanes and cut == [False, False] and dropped is False


def test_fit_lanes_drops_the_droppable_field_before_items():
    from utils.response_budget import fit_lanes

    lanes = [[{"id": i, "cs": "x" * 50} for i in range(3)]]
    lean = [{"id": i} for i in range(3)]
    budget = sum(json_chars(item) + 1 for item in lean)
    out, cut, dropped = fit_lanes(lanes, budget, droppable="cs")
    assert out == [lean] and cut == [False] and dropped is True


def test_fit_lanes_gives_the_first_lane_priority():
    from utils.response_budget import fit_lanes

    first = [{"id": i, "v": "x" * 20} for i in range(5)]
    second = [{"id": i, "v": "y" * 20} for i in range(5)]
    budget = sum(json_chars(item) + 1 for item in first) + json_chars(second[0]) + 1
    out, cut, dropped = fit_lanes([first, second], budget)
    assert out[0] == first and out[1] == second[:1]
    assert cut == [False, True] and dropped is False
