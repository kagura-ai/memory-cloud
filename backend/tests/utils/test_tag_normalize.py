"""Tag folding for drift-tolerant reads (#1503).

The two relations are tested separately because they have different jobs:
``normalize_tag`` widens a real filter and must be conservative; the folded
forms of two tags an author meant differently must NOT collide.
``is_near_duplicate`` only produces advisory hints and is deliberately looser.
"""

import pytest

from utils.tag_normalize import (
    is_mechanical_variant,
    is_near_duplicate,
    is_specialisation,
    normalize_tag,
)


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


class TestJapaneseKanaFold:
    """#2028: hiragana / katakana and a word-final long-vowel mark fold together.

    Both rules are character-level, with no dictionary. Hiragana (and ゝ ゞ)
    maps to katakana right after NFKC + casefold. After separator removal, one
    trailing ``ー`` is dropped when it directly follows a katakana LETTER
    (ァ-ヺ) and the final katakana word — the trailing run of letters and marks,
    starting at a letter — is at least 3 characters (small kana count as one).
    """

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("ログ", "ろぐ"),
            ("デプロイ", "でぷろい"),
            ("バグ", "ばぐ"),
            ("テスト", "てすと"),
            ("サーバー", "サーバ"),
            ("ユーザー", "ユーザ"),
            ("メモリ", "メモリー"),
            # Both rules together, and the existing half-width kana fold.
            ("さーばー", "サーバ"),
            ("デプロイ", "ﾃﾞﾌﾟﾛｲ"),
            ("ｻｰﾊﾞｰ", "サーバ"),
        ],
    )
    def test_kana_variants_fold_together(self, a, b):
        assert normalize_tag(a) == normalize_tag(b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # Too short to tell a dropped mark from a different word.
            ("ゲー", "ゲ"),
            # A word-internal mark is out of scope (it needs a dictionary).
            ("データーベース", "データベース"),
            # Kanji vs its reading is a homophone hazard, never folded.
            ("障害", "しょうがい"),
        ],
    )
    def test_out_of_scope_pairs_stay_apart(self, a, b):
        assert normalize_tag(a) != normalize_tag(b)

    @pytest.mark.parametrize(
        ("tag", "folded"),
        [
            ("ー", "ー"),
            ("ゲー", "ゲー"),
            ("データーベース", "データーベース"),
            ("障害", "障害"),
            ("しょうがい", "ショウガイ"),
            # A doubled mark is not a long vowel after a letter: the mark before
            # the last one is not a katakana LETTER, so nothing is dropped and
            # the fold stays idempotent.
            ("サーバーー", "サーバーー"),
            # The final katakana word is what must be 3+ characters, not the tag.
            ("category:ゲー", "category:ゲー"),
            ("category:サーバー", "category:サーバ"),
            # Small kana count as one character each: these are 3-character words.
            ("ヴァー", "ヴァ"),
            ("ジャー", "ジャ"),
            ("ティー", "ティ"),
            ("チャー", "チャ"),
            # A leading mark is not part of the final word, which starts at a letter.
            ("ーサー", "ーサー"),
            ("ーサーバー", "ーサーバ"),
            # Iteration marks map with the hiragana.
            ("いすゞ", "イスヾ"),
            # A mark after a non-katakana character stays.
            ("abcー", "abcー"),
            ("障害ー", "障害ー"),
        ],
    )
    def test_the_fold_is_exact(self, tag, folded):
        assert normalize_tag(tag) == folded

    @pytest.mark.parametrize(
        "tag", ["サーバー", "サーバーー", "ゲー", "category:サーバー", "さーばー", "スキー"]
    )
    def test_the_fold_is_idempotent(self, tag):
        assert normalize_tag(normalize_tag(tag)) == normalize_tag(tag)

    def test_a_separator_never_changes_the_long_vowel_drop(self):
        """The drop runs after separator removal: a trailing or leading
        separator neither hides the final mark nor shortens the word."""
        assert normalize_tag("サーバー-") == normalize_tag("サーバー") == normalize_tag("サーバ")
        assert normalize_tag("-ゲー") == normalize_tag("ゲー")
        assert normalize_tag("-ゲー") != normalize_tag("ゲ")

    def test_a_short_final_word_keeps_its_mark_after_a_prefix(self):
        assert normalize_tag("category:ゲー") != normalize_tag("category:ゲ")
        assert normalize_tag("genre:ゲー") != normalize_tag("genre:ゲ")
        assert normalize_tag("category:サーバー") == normalize_tag("category:サーバ")

    @pytest.mark.parametrize("mark", ["\u30a0", "\u30fb", "\u30fd", "\u30fe"])
    def test_a_mark_after_a_non_letter_katakana_character_stays(self, mark):
        """゠ ・ ヽ ヾ sit in the katakana block but are not letters. (ヿ is not
        listed: NFKC rewrites it to the letters コト.)"""
        tag = "サーバ" + mark + "ー"
        assert normalize_tag(tag) == tag

    def test_iteration_marks_fold_across_scripts(self):
        assert normalize_tag("いすゞ") == normalize_tag("イスヾ")
        assert normalize_tag("こゝろ") == normalize_tag("コヽロ")

    @pytest.mark.parametrize(
        "tag", ["dev-environment", "Troubleshooting", "issues", "v0.73.0", "redis", "鯖"]
    )
    def test_latin_and_kanji_tags_fold_as_before(self, tag):
        """No kana in the tag: the fold is exactly the pre-#2028 pipeline."""
        import unicodedata

        from utils.tag_normalize import _SEPARATORS, _strip_plural

        before = _strip_plural(
            _SEPARATORS.sub("", unicodedata.normalize("NFKC", tag).strip().casefold())
        )
        assert normalize_tag(tag) == before

    def test_segments_drop_the_mark_on_the_final_segment_only(self):
        """``_segments`` agrees with ``normalize_tag``: an inner segment keeps its
        mark (the whole fold keeps it too), the final segment loses it."""
        from utils.tag_normalize import _segments

        assert _segments("サーバー-ログ") == ["サーバー", "ログ"]
        assert _segments("ログ-サーバー") == ["ログ", "サーバ"]
        assert _segments("サーバー-") == ["サーバ"]
        assert normalize_tag("サーバー-ログ") != normalize_tag("サーバ-ログ")
        assert not is_mechanical_variant("サーバー-ログ", "サーバ-ログ")
        assert not is_specialisation("サーバー-ログ", "サーバ-ログ")
        # One mark apart in a long tag: a typo-distance hint, not a variant.
        assert is_near_duplicate("サーバー-ログ", "サーバ-ログ")
        # The final-segment drop keeps compounds ending in the word consistent.
        assert is_specialisation("ログ-サーバー", "ログ")
        assert is_mechanical_variant("ログ-サーバー", "ろぐ-さーば")

    def test_kana_variants_are_mechanical_variants_and_near_duplicates(self):
        assert is_mechanical_variant("ろぐ", "ログ")
        assert is_near_duplicate("サーバー", "サーバ")

    def test_the_digit_skeleton_fold_sees_the_kana_fold(self):
        """``_digit_skeleton`` folds through ``normalize_tag``, so a kana series
        is still a series and the same number in the other script still hints."""
        assert not is_near_duplicate("すぷりんと1", "スプリント2")
        assert is_near_duplicate("すぷりんと1", "スプリント1")
        assert not is_near_duplicate("ふぇーず-1", "フェーズ-2")


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
            ("sprint¹²", "sprint³⁴"),  # superscripts are not \d until NFKC folds them
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
            # Same VALUE, different zero padding: the folds differ, but it is one
            # identifier written two ways — the drift the lint exists for — so
            # the series rule steps aside and the typo rule still relates them.
            ("sprint-07", "sprint-7"),
            ("2026-9-5", "2026-09-05"),
            ("issue:#0179", "issue:#179"),
            ("ｓｐｒｉｎｔ－０７", "sprint-7"),
        ],
    )
    def test_the_same_number_padded_differently_is_left_to_the_other_rules(self, a, b):
        assert is_near_duplicate(a, b)
        assert is_near_duplicate(b, a)

    def test_padding_is_compared_per_number_not_per_tag(self):
        """One padded field does not excuse a different value in another field."""
        assert not is_near_duplicate("2026-09-05", "2026-9-06")
        # Only LEADING zeros are padding: 1.10 is not 1.1.
        assert not is_near_duplicate("release-1.10", "release-1.1")

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
            ("session-2026-09", "session-2026-09-21", True),  # prefix: month vs day tag
            ("v0.7", "v0.73.0", False),  # folds to 'v07' — too short for either
        ],
    )
    def test_a_different_field_count_is_left_to_the_other_rules(self, a, b, expected):
        """The mask runs before separators are dropped, so '0.73.0' stays 3 fields."""
        assert is_near_duplicate(a, b) is expected
        assert is_near_duplicate(b, a) is expected


class TestIsSpecialisation:
    """#1617: a compound tag and its own leading segment(s) are a topic and a
    sub-topic, not two spellings of one tag. The predicate is pure, symmetric and
    reads no vocabulary; the write lint uses it to drop such matches, the read
    path deliberately does not.
    """

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # Class A from the issue: compound vs its generic leading segment(s).
            ("session-cookie", "session"),
            ("deploy-checklist", "deploy"),
            ("cache-layer-redis", "cache-layer"),
            ("post-deploy-check", "post-deploy"),
            ("search-response-size", "search"),
            ("changelog-date-utc", "changelog"),
            # Separator and case drift between the two sides.
            ("NEXT_PUBLIC_PLAN_DISPLAY_NAMES", "next-public"),
            ("Session Cookie", "session"),
            # Class B: a dated / numbered member vs the bare series name.
            ("session-2026-09-11", "session"),
            ("s3-bucket-2", "s3-bucket"),
            # ':' and '#' are segment boundaries too.
            ("category:auth", "category"),
            ("some-repo#62", "some-repo"),
            ("issue:#1599", "issue"),
            # Plural is folded per segment: 'sessions' is the generic of 'session-cookie'.
            ("sessions", "session-cookie"),
            # Full-width separators split after NFKC; non-Latin segments work too.
            ("ｓｅｓｓｉｏｎ－ｃｏｏｋｉｅ", "session"),
            ("セッション-クッキー", "セッション"),
            # A punctuation-only segment carries no signal and is dropped.
            ("session-.-cookie", "session"),
        ],
    )
    def test_a_compound_and_its_leading_segments_are_a_specialisation(self, a, b):
        assert is_specialisation(a, b)
        assert is_specialisation(b, a)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # Equal whole folds are mechanical variants, never a specialisation —
            # even when one side has an extra separator-delimited segment.
            ("deploy", "deploy-s"),
            ("session", "session-"),
            ("Dev_Environment", "dev-environment"),
            ("category:auth", "category-auth"),
            ("auth-n", "authn"),
            ("plan-tier", "plan-tiers"),
            # A partial last segment is an abbreviation, which the prefix rule
            # exists for: the boundary has to be a WHOLE-segment boundary.
            ("dev-env", "dev-environment"),
            ("deploy-check", "deploy-checklist"),
            ("category:auth", "category:authn"),
            # Single-segment pairs have no boundary to speak of.
            ("oauth", "oauth2"),
            ("kube", "kubernetes"),
            ("auth", "authn"),
            # '.' is NOT a boundary: a dotted name is one identifier.
            ("node", "node.js"),
            ("next", "next.js"),
            ("socket", "socket.io"),
            ("v0.73", "v0.73.0"),
            # Same number of segments, different content.
            ("isue:#1599", "issue:#1599"),
            ("sprint-07", "sprint-7"),
            ("python", "javascript"),
            # Numeric-precision exception: the shorter tag ends in a digit and
            # every extra segment is a number — one identifier at two precisions.
            ("session-2026-09", "session-2026-09-21"),
            ("2026-09", "2026-09-21"),
            ("release-1", "release-1-2"),
        ],
    )
    def test_not_a_specialisation(self, a, b):
        assert not is_specialisation(a, b)
        assert not is_specialisation(b, a)

    def test_numeric_exception_looks_only_at_the_last_segment(self):
        """A digit elsewhere in the shorter tag does not make the extra number a
        precision step: ``s3-bucket-2`` is a member of the ``s3-bucket`` series."""
        assert is_specialisation("s3-bucket", "s3-bucket-2")
        # A non-numeric extra segment is never a precision step, whatever the
        # shorter tag ends in.
        assert is_specialisation("s3", "s3-bucket")
        assert is_specialisation("2026-09", "2026-09-retro")

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("auth", "auth-n"),
            ("front", "front-end"),
            ("mongo", "mongo-db"),
            ("python", "python-3"),
            ("oauth", "oauth-2.0"),
        ],
    )
    def test_a_respelling_that_ends_on_a_segment_boundary_is_an_accepted_trade_off(self, a, b):
        """Structurally identical to ``java`` / ``java-script``, which must be a
        specialisation; the boundary is the only mechanical signal available.
        The unseparated spellings (``authn``, ``frontend``, ...) are unaffected.
        """
        assert is_specialisation(a, b)
        assert not is_specialisation(a, b.replace("-", ""))

    def test_empty_fold_is_never_a_specialisation(self):
        assert not is_specialisation("---", "python")
        assert not is_specialisation("###", "python-3")
        assert not is_specialisation("---", "===")

    def test_the_predicate_is_independent_of_is_near_duplicate(self):
        """Both relations hold for a class A pair: the write lint subtracts one
        from the other, the read path keeps the near-duplicate as is."""
        assert is_near_duplicate("session-cookie", "session")
        assert is_specialisation("session-cookie", "session")


class TestIsMechanicalVariant:
    """The WRITE-side relation (#1871): one fold AND the same numbers.

    ``normalize_tag`` deletes separators, so it is not injective on tags that
    carry numbers (``v0.11.0`` and ``v0.1.10`` both fold to ``v0110``). Widening
    a read filter by it is recoverable; storing a memory under the other tag is
    not, so the write path also requires the digit runs to be the same.
    """

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Dev_Environment", "dev-environment"),
            ("dev environment", "dev.environment"),
            ("V0.94.0", "v0.94.0"),
            ("v0_94_0", "v0.94.0"),
            ("PR-123", "pr_123"),
            ("pr123", "pr-123"),  # a separator between letters and a number changes no number
            ("issues", "issue"),
            ("release-2-notes", "release-2-note"),
            ("ｖ０．９４．０", "v0.94.0"),  # full-width digits are the same digits after NFKC
            ("python", "python"),
        ],
    )
    def test_same_fold_and_same_numbers_is_a_variant(self, a, b):
        assert is_mechanical_variant(a, b)
        assert is_mechanical_variant(b, a)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # The four pairs from the issue: one fold, different identifiers.
            ("v0.11.0", "v0.1.10"),
            ("v0.94", "v0.9.4"),
            ("pr-123", "pr-1-23"),
            ("2026-11-2", "2026-1-12"),
            # Same digits, same number of fields, split at a different place.
            ("v0.01", "v00.1"),
            ("1-23-4", "12-3-4"),
            ("ｖ０．１１．０", "v0.1.10"),
        ],
    )
    def test_same_fold_with_different_numbers_is_not_a_variant(self, a, b):
        assert normalize_tag(a) == normalize_tag(b)  # the collision this guards
        assert not is_mechanical_variant(a, b)
        assert not is_mechanical_variant(b, a)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("dev-env", "dev-environment"),  # abbreviation
            ("troubleshoot", "troubleshooting"),
            ("sprint-07", "sprint-7"),  # zero padding is not folded
            ("v0.73.0", "v0.69.0"),
            ("python", "javascript"),
        ],
    )
    def test_different_folds_are_not_variants(self, a, b):
        assert not is_mechanical_variant(a, b)
        assert not is_mechanical_variant(b, a)

    def test_a_tag_with_no_folding_signal_is_a_variant_of_nothing(self):
        assert not is_mechanical_variant("---", "...")
        assert not is_mechanical_variant("---", "---")
