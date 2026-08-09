"""Tag folding for drift-tolerant reads (#1503).

The two relations are tested separately because they have different jobs:
``normalize_tag`` widens a real filter and must be conservative; the folded
forms of two tags an author meant differently must NOT collide.
``is_near_duplicate`` only produces advisory hints and is deliberately looser.
"""

import pytest

from utils.tag_normalize import is_near_duplicate, normalize_tag


class TestNormalizeTag:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("dev-environment", "dev_environment"),
            ("dev-environment", "Dev-Environment"),
            ("dev-environment", "dev environment"),
            ("dev-environment", "devenvironment"),
            ("dev-environment", "dev.environment"),
            ("Troubleshooting", "troubleshooting"),
            ("category:auth", "Category:Auth"),
            ("  python  ", "python"),
        ],
    )
    def test_mechanical_variants_fold_together(self, a, b):
        assert normalize_tag(a) == normalize_tag(b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # The issue's own example: an abbreviation is NOT a mechanical
            # variant, and must not silently widen a filter.
            ("dev-env", "dev-environment"),
            ("auth", "authentication"),
            ("python", "python3"),
            ("api", "apic"),
            # Distinct concepts that happen to be close.
            ("prod", "prodigy"),
        ],
    )
    def test_distinct_tags_do_not_fold_together(self, a, b):
        assert normalize_tag(a) != normalize_tag(b)

    @pytest.mark.parametrize(
        ("plural", "singular"),
        [("issues", "issue"), ("memories", "memory"), ("tags", "tag")],
    )
    def test_simple_plurals_fold_to_singular(self, plural, singular):
        assert normalize_tag(plural) == normalize_tag(singular)

    @pytest.mark.parametrize("tag", ["class", "progress", "css", "bus"])
    def test_double_s_and_short_words_keep_their_s(self, tag):
        """Stripping these would collide with unrelated tags ('clas', 'bu')."""
        assert normalize_tag(tag) == tag.casefold()

    def test_full_width_latin_folds_to_half_width(self):
        """NFKC, matching how searchable text is normalized elsewhere."""
        assert normalize_tag("ｐｙｔｈｏｎ") == normalize_tag("python")

    def test_japanese_tags_are_untouched_apart_from_case_and_separators(self):
        assert normalize_tag("鯖") == "鯖"
        assert normalize_tag("味噌 煮") == "味噌煮"

    def test_punctuation_only_tag_folds_to_empty(self):
        """An empty fold must never be used to match — callers check for it."""
        assert normalize_tag("---") == ""
        assert normalize_tag("   ") == ""


class TestIsNearDuplicate:
    def test_the_issue_example_is_suggested(self):
        """dev-env / dev-environment: unreachable by folding, caught here."""
        assert is_near_duplicate("dev-environment", "dev-env")
        assert is_near_duplicate("dev-env", "dev-environment")

    def test_mechanical_variants_are_also_suggested(self):
        assert is_near_duplicate("dev-environment", "Dev_Environment")

    def test_typos_within_two_edits_are_suggested(self):
        assert is_near_duplicate("troubleshooting", "troubleshootng")
        assert is_near_duplicate("authentication", "authentcation")

    def test_short_tags_do_not_suggest_everything_sharing_a_prefix(self):
        """'ci' must not pull in every tag starting with those letters."""
        assert not is_near_duplicate("ci", "circleci")
        assert not is_near_duplicate("db", "dbt")
        assert not is_near_duplicate("go", "google")

    def test_short_tags_do_not_suggest_on_edit_distance(self):
        """One edit between 3-char tags relates too many unrelated tags."""
        assert not is_near_duplicate("cat", "car")
        assert not is_near_duplicate("api", "apt")

    def test_unrelated_tags_are_not_suggested(self):
        assert not is_near_duplicate("python", "javascript")
        assert not is_near_duplicate("category:auth", "deployment")

    def test_empty_fold_never_matches(self):
        assert not is_near_duplicate("---", "python")
        assert not is_near_duplicate("---", "===")
