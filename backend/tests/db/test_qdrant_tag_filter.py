"""Tests for tag filtering and BM25 search integration.

Issue #67: Verify tags are properly indexed, filtered, and searched.
Issue #79: Verify AND logic for tag filters (tags_match="all").
"""

from qdrant_client.models import FieldCondition, MatchAny, MatchValue

from db.qdrant import _build_tag_filter_conditions


class TestBuildTagFilterConditions:
    """Test _build_tag_filter_conditions helper."""

    def test_with_tags_list(self):
        """Should return FieldCondition with MatchAny for valid tags (default OR)."""
        filters = {"tags": ["python", "fastapi"]}
        conditions = _build_tag_filter_conditions(filters)

        assert len(conditions) == 1
        assert isinstance(conditions[0], FieldCondition)
        assert conditions[0].key == "tags"
        assert isinstance(conditions[0].match, MatchAny)
        assert conditions[0].match.any == ["python", "fastapi"]

    def test_with_single_tag(self):
        """Should work with single-element list."""
        conditions = _build_tag_filter_conditions({"tags": ["auth"]})
        assert len(conditions) == 1
        assert conditions[0].match.any == ["auth"]

    def test_with_empty_list(self):
        """Empty tags list should return empty list."""
        assert _build_tag_filter_conditions({"tags": []}) == []

    def test_with_no_tags_key(self):
        """Missing tags key should return empty list."""
        assert _build_tag_filter_conditions({"type": "code"}) == []

    def test_with_empty_filters(self):
        """Empty filters dict should return empty list."""
        assert _build_tag_filter_conditions({}) == []

    def test_with_none_tags(self):
        """None tags value should return empty list."""
        assert _build_tag_filter_conditions({"tags": None}) == []

    def test_with_string_tags(self):
        """String (not list) should return empty list — must be a list."""
        assert _build_tag_filter_conditions({"tags": "python"}) == []

    def test_with_japanese_tags(self):
        """Japanese tags should work correctly."""
        filters = {"tags": ["category:料理", "鯖", "サバ"]}
        conditions = _build_tag_filter_conditions(filters)

        assert len(conditions) == 1
        assert conditions[0].match.any == ["category:料理", "鯖", "サバ"]

    def test_with_category_tags(self):
        """Category-prefixed tags should work."""
        filters = {"tags": ["category:backend", "category:auth"]}
        conditions = _build_tag_filter_conditions(filters)

        assert len(conditions) == 1
        assert len(conditions[0].match.any) == 2


class TestTagFilterAndLogic:
    """Test AND logic for tag filters (Issue #79)."""

    def test_tags_match_all(self):
        """tags_match='all' should return one MatchValue per tag."""
        filters = {"tags": ["python", "fastapi"], "tags_match": "all"}
        conditions = _build_tag_filter_conditions(filters)

        assert len(conditions) == 2
        assert all(isinstance(c, FieldCondition) for c in conditions)
        assert all(c.key == "tags" for c in conditions)
        assert isinstance(conditions[0].match, MatchValue)
        assert isinstance(conditions[1].match, MatchValue)
        assert conditions[0].match.value == "python"
        assert conditions[1].match.value == "fastapi"

    def test_tags_match_any_explicit(self):
        """tags_match='any' should use MatchAny (same as default)."""
        filters = {"tags": ["python", "fastapi"], "tags_match": "any"}
        conditions = _build_tag_filter_conditions(filters)

        assert len(conditions) == 1
        assert isinstance(conditions[0].match, MatchAny)

    def test_tags_match_default_is_any(self):
        """Without tags_match, default should be OR (MatchAny)."""
        filters_with = {"tags": ["a", "b"], "tags_match": "any"}
        filters_without = {"tags": ["a", "b"]}

        conditions_with = _build_tag_filter_conditions(filters_with)
        conditions_without = _build_tag_filter_conditions(filters_without)

        assert len(conditions_with) == len(conditions_without) == 1
        assert isinstance(conditions_with[0].match, MatchAny)
        assert isinstance(conditions_without[0].match, MatchAny)

    def test_tags_match_all_single_tag(self):
        """AND with single tag should return one MatchValue."""
        filters = {"tags": ["python"], "tags_match": "all"}
        conditions = _build_tag_filter_conditions(filters)

        assert len(conditions) == 1
        assert isinstance(conditions[0].match, MatchValue)
        assert conditions[0].match.value == "python"

    def test_tags_match_all_japanese(self):
        """AND logic should work with Japanese tags."""
        filters = {"tags": ["予算", "2026"], "tags_match": "all"}
        conditions = _build_tag_filter_conditions(filters)

        assert len(conditions) == 2
        assert conditions[0].match.value == "予算"
        assert conditions[1].match.value == "2026"


class TestTagFilterIntegration:
    """Test that tag filtering works as exact-match only (not BM25)."""

    def test_filter_does_not_affect_bm25_scoring(self):
        """Tag filter uses MatchAny (exact), not MatchText (BM25)."""
        conditions = _build_tag_filter_conditions({"tags": ["python"]})
        # Must use MatchAny, NOT MatchText
        assert isinstance(conditions[0].match, MatchAny)

    def test_multiple_tags_any_match(self):
        """MatchAny matches if ANY tag in the list is present."""
        conditions = _build_tag_filter_conditions({"tags": ["引越し", "ひっこし"]})
        assert len(conditions[0].match.any) == 2
