"""``compact_summary`` at the bootstrap index limit (#2126).

Recall's compact rows use the same helper at its default 160; these cases pin
the cut rule at ``INDEX_SUMMARY_CHARS`` (80).
"""

from __future__ import annotations

from utils.recall_projection import COMPACT_SUMMARY_CHARS, INDEX_SUMMARY_CHARS, compact_summary


def test_the_limits():
    assert INDEX_SUMMARY_CHARS == 80
    assert COMPACT_SUMMARY_CHARS == 160


def test_exactly_the_limit_is_returned_as_stored():
    text = "a" * 80
    assert compact_summary(text, INDEX_SUMMARY_CHARS) == (text, False)


def test_one_over_with_a_space_past_the_half_cuts_at_the_word():
    text = "x" * 60 + " " + "y" * 20  # 81 chars, space at index 60
    head, cut = compact_summary(text, INDEX_SUMMARY_CHARS)
    assert cut is True
    assert head == "x" * 60 + "…"
    assert len(head) <= INDEX_SUMMARY_CHARS


def test_a_single_long_token_is_cut_mid_token():
    url = "https://example.com/" + "p" * 120
    head, cut = compact_summary(url, INDEX_SUMMARY_CHARS)
    assert cut is True
    assert len(head) == INDEX_SUMMARY_CHARS and head.endswith("…")
    assert head.startswith("https://example.com/")


def test_a_space_in_the_first_half_does_not_collapse_the_head():
    text = "see " + "z" * 120
    head, cut = compact_summary(text, INDEX_SUMMARY_CHARS)
    assert cut is True and len(head) == INDEX_SUMMARY_CHARS


def test_whitespace_only_overflow_is_flattened_not_cut():
    text = "word\n\n   " * 9  # 90 chars raw, "word word ..." 44 flat
    head, cut = compact_summary(text, INDEX_SUMMARY_CHARS)
    assert cut is False
    assert head == " ".join(["word"] * 9)


def test_the_ellipsis_counts_toward_the_limit():
    for n in range(81, 300, 7):
        head, cut = compact_summary("ab " * n, INDEX_SUMMARY_CHARS)
        assert cut is True and len(head) <= INDEX_SUMMARY_CHARS


def test_recall_default_is_unchanged():
    text = "w " * 100
    head, cut = compact_summary(text)
    assert cut is True and len(head) <= COMPACT_SUMMARY_CHARS


def test_a_space_exactly_at_the_half_still_cuts_at_the_word():
    text = "x" * 40 + " " + "y" * 60  # space at index 40 == limit // 2
    head, cut = compact_summary(text, INDEX_SUMMARY_CHARS)
    assert cut is True and head == "x" * 40 + "…"
    text = "x" * 39 + " " + "y" * 60  # space at index 39: first half → mid-token cut
    head, cut = compact_summary(text, INDEX_SUMMARY_CHARS)
    assert cut is True and len(head) == INDEX_SUMMARY_CHARS
