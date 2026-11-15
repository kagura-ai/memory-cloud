"""``utils.list_search``: the ``search`` parameter of an admin list (#2841, #2831).

No database: the normalisation and the LIKE pattern are pure functions. The
route tests pin them again on real rows.
"""

from __future__ import annotations

import pytest

from utils.list_search import (
    LIKE_ESCAPE,
    normalize_search,
    search_validator,
    substring_pattern,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("\t\n ", None),
        ("abc", "abc"),
        ("  Abc  ", "Abc"),
        # Whitespace around the text goes with the trim; inside it stays.
        ("\n abc def \t", "abc def"),
        ("50%", "50%"),
        ("_", "_"),
        ("\\", "\\"),
    ],
)
def test_normalize_trims_and_empty_means_no_filter(raw: str | None, expected: str | None) -> None:
    assert normalize_search(raw, 255) == expected


def test_normalize_accepts_the_column_width_and_refuses_one_more() -> None:
    assert normalize_search("x" * 255, 255) == "x" * 255
    with pytest.raises(ValueError, match="at most 255 characters"):
        normalize_search("x" * 256, 255)


def test_normalize_counts_the_length_before_the_trim() -> None:
    """A value longer than the column is refused, whatever a trim would leave."""
    with pytest.raises(ValueError, match="at most 10 characters"):
        normalize_search(" " * 10 + "x", 10)


@pytest.mark.parametrize(
    "control",
    ["\x00", "\x01", "\t", "\n", "\r", "\x1b", "\x1f", "\x7f", "\x80", "\x85", "\x9f"],
    ids=lambda char: f"U+{ord(char):04X}",
)
def test_normalize_refuses_a_control_character_inside_the_text(control: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        normalize_search(f"a{control}b", 255)


def test_normalize_keeps_text_that_is_not_a_control_character() -> None:
    for text in ("a b", "日本語 テスト", "naïve café", "a b", "x@example.com"):
        assert normalize_search(text, 255) == text


@pytest.mark.parametrize("raw", ["secret-sentinel\x00", "s" * 30])
def test_a_refusal_never_repeats_the_text(raw: str) -> None:
    with pytest.raises(ValueError) as refusal:
        normalize_search(raw, 20)
    assert "sentinel" not in str(refusal.value)
    assert "sss" not in str(refusal.value)


def test_search_validator_binds_the_length() -> None:
    validate = search_validator(5)
    assert validate("  abcde  "[:5]) == "abc"
    assert validate("abcde") == "abcde"
    assert validate(None) is None
    with pytest.raises(ValueError, match="at most 5 characters"):
        validate("abcdef")


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("abc", "%abc%"),
        ("50%", "%50\\%%"),
        ("_", "%\\_%"),
        ("a_b%c", "%a\\_b\\%c%"),
        ("\\", "%\\\\%"),
        # The backslash is escaped first: the escapes added for % and _ are
        # not escaped again.
        ("\\%", "%\\\\\\%%"),
        ("\\_", "%\\\\\\_%"),
    ],
)
def test_substring_pattern_escapes_the_like_wildcards(text: str, pattern: str) -> None:
    assert LIKE_ESCAPE == "\\"
    assert substring_pattern(text) == pattern
