"""Tests for tag filtering and BM25 search integration.

Issue #67: Verify tags are properly indexed, filtered, and searched.
"""

from qdrant_client.models import FieldCondition, MatchAny

from db.qdrant import _build_tag_filter_condition


class TestBuildTagFilterCondition:
    """Test _build_tag_filter_condition helper."""

    def test_with_tags_list(self):
        """Should return FieldCondition with MatchAny for valid tags."""
        filters = {"tags": ["python", "fastapi"]}
        condition = _build_tag_filter_condition(filters)

        assert condition is not None
        assert isinstance(condition, FieldCondition)
        assert condition.key == "tags"
        assert isinstance(condition.match, MatchAny)
        assert condition.match.any == ["python", "fastapi"]

    def test_with_single_tag(self):
        """Should work with single-element list."""
        condition = _build_tag_filter_condition({"tags": ["auth"]})
        assert condition is not None
        assert condition.match.any == ["auth"]

    def test_with_empty_list(self):
        """Empty tags list should return None."""
        assert _build_tag_filter_condition({"tags": []}) is None

    def test_with_no_tags_key(self):
        """Missing tags key should return None."""
        assert _build_tag_filter_condition({"type": "code"}) is None

    def test_with_empty_filters(self):
        """Empty filters dict should return None."""
        assert _build_tag_filter_condition({}) is None

    def test_with_none_tags(self):
        """None tags value should return None."""
        assert _build_tag_filter_condition({"tags": None}) is None

    def test_with_string_tags(self):
        """String (not list) should return None — must be a list."""
        assert _build_tag_filter_condition({"tags": "python"}) is None

    def test_with_japanese_tags(self):
        """Japanese tags should work correctly."""
        filters = {"tags": ["category:料理", "鯖", "サバ"]}
        condition = _build_tag_filter_condition(filters)

        assert condition is not None
        assert condition.match.any == ["category:料理", "鯖", "サバ"]

    def test_with_category_tags(self):
        """Category-prefixed tags should work."""
        filters = {"tags": ["category:backend", "category:auth"]}
        condition = _build_tag_filter_condition(filters)

        assert condition is not None
        assert len(condition.match.any) == 2


class TestTagFilterIntegration:
    """Test that tag filtering works as exact-match only (not BM25)."""

    def test_filter_does_not_affect_bm25_scoring(self):
        """Tag filter uses MatchAny (exact), not MatchText (BM25)."""
        from qdrant_client.models import MatchAny

        condition = _build_tag_filter_condition({"tags": ["python"]})
        # Must use MatchAny, NOT MatchText
        assert isinstance(condition.match, MatchAny)

    def test_multiple_tags_any_match(self):
        """MatchAny matches if ANY tag in the list is present."""
        condition = _build_tag_filter_condition({"tags": ["引越し", "ひっこし"]})
        assert len(condition.match.any) == 2
