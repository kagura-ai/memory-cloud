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

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("redis", "redi"),
            ("https", "http"),
            ("status", "statu"),
            ("chaos", "chao"),
            ("alias", "alia"),
        ],
    )
    def test_non_plural_endings_are_not_stripped(self, a, b):
        """Over-stripping merges tags an author kept distinct (review finding)."""
        assert normalize_tag(a) != normalize_tag(b)

    def test_real_plurals_still_fold(self):
        for plural, singular in [("tags", "tag"), ("issues", "issue"), ("memories", "memory")]:
            assert normalize_tag(plural) == normalize_tag(singular)

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

    @pytest.mark.parametrize(
        ("a", "b"),
        [("test", "latest"), ("prod", "reprod"), ("auth", "oauth")],
    )
    def test_shared_suffix_alone_does_not_suggest(self, a, b):
        """A suffix match is weak evidence of abbreviation and adds noise.

        The case this rule exists for — dev-env / dev-environment — is a prefix.
        """
        assert not is_near_duplicate(a, b)

    def test_unrelated_tags_are_not_suggested(self):
        assert not is_near_duplicate("python", "javascript")
        assert not is_near_duplicate("category:auth", "deployment")

    def test_empty_fold_never_matches(self):
        assert not is_near_duplicate("---", "python")
        assert not is_near_duplicate("---", "===")


class TestNumberedSeriesAreNotNearDuplicates:
    """#1608: tags that differ only in their numbers are distinct identifiers.

    ``issue:#1599`` is not a misspelling of ``issue:#179``. Workspaces that tag
    by issue, PR, version or date otherwise get a hint on almost every write,
    which is how an agent learns to ignore the lint.
    """

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # The four pairs from the issue.
            ("issue:#1599", "issue:#179"),
            ("v0.73.0", "v0.69.0"),
            ("session-2026-09-21", "session-2026-05-20"),
            ("pr:#1607", "pr:#601"),
            # One series member being a prefix of another is still a series.
            ("issue:#15", "issue:#1599"),
            # Bare dates, and names that carry a version.
            ("2026-09", "2026-08"),
            ("python3", "python2"),
            ("gpt-4o", "gpt-5o"),
            # Full-width digits fold to half-width before the comparison.
            ("ｖ０．７３．０", "v0.69.0"),
            # Separator and case drift does not hide the series.
            ("Session_2026_09_21", "session-2026-05-20"),
        ],
    )
    def test_same_series_different_numbers_is_not_suggested(self, a, b):
        assert not is_near_duplicate(a, b)
        assert not is_near_duplicate(b, a)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # Same number written two ways: equal after folding, still related.
            ("ｖ１．２", "v1.2"),
            ("Issue:#1599", "issue:#1599"),
            ("session-2026-09-21", "session_2026_09_21"),
        ],
    )
    def test_the_same_number_spelled_differently_is_still_suggested(self, a, b):
        assert is_near_duplicate(a, b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # Letters differ as well as (or instead of) digits: a real typo or
            # abbreviation, so the prefix and edit-distance rules still decide.
            ("isue:#1599", "issue:#1599"),
            ("oauth", "oauth2"),
            ("gpt-4o", "gpt-4"),
            ("kuberentes", "kubernetes"),
        ],
    )
    def test_a_difference_in_letters_still_goes_through_the_other_rules(self, a, b):
        assert is_near_duplicate(a, b)
        assert is_near_duplicate(b, a)

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            # A different NUMBER of numeric fields is not the same series, so
            # these are left to the prefix and edit-distance rules unchanged.
            ("v0.73", "v0.73.0", True),  # prefix: plausibly the same release
            ("oauth2", "oauth-2.0", True),  # prefix: the same thing, two ways
            ("v0.7", "v0.73.0", False),  # folds to 'v07' — too short for either
        ],
    )
    def test_a_different_field_count_is_left_to_the_other_rules(self, a, b, expected):
        """The mask runs before separators are dropped, so '0.73.0' stays 3 fields."""
        assert is_near_duplicate(a, b) is expected
        assert is_near_duplicate(b, a) is expected
