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
