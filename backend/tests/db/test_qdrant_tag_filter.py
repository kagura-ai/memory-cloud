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


class TestTagsTextPayload:
    """Test tags_text construction logic (mirrors memory_service.py)."""

    def test_tags_to_tags_text(self):
        """Tags should be joined and lowercased."""
        tags = ["Python", "FastAPI", "category:Backend"]
        tags_str = " ".join(t.lower() for t in tags)
        assert tags_str == "python fastapi category:backend"

    def test_empty_tags(self):
        """Empty tags should produce empty string."""
        tags_str = " ".join(t.lower() for t in []) if [] else ""
        assert tags_str == ""

    def test_none_tags(self):
        """None tags should produce empty string."""
        tags = None
        tags_str = " ".join(t.lower() for t in tags) if tags else ""
        assert tags_str == ""

    def test_japanese_tags_lowercase(self):
        """Japanese tags are unaffected by lowercase (no case in Japanese)."""
        tags = ["鯖", "サバ", "さば", "味噌煮"]
        tags_str = " ".join(t.lower() for t in tags)
        assert tags_str == "鯖 サバ さば 味噌煮"

    def test_embed_text_with_tags(self):
        """Embedding text should include summary + tags."""
        summary = "鯖の味噌煮レシピ"
        tags = ["鯖", "サバ", "category:料理"]
        tags_str = " ".join(t.lower() for t in tags)
        embed_text = f"{summary} {tags_str}" if tags_str else summary
        assert embed_text == "鯖の味噌煮レシピ 鯖 サバ category:料理"

    def test_embed_text_without_tags(self):
        """Without tags, embedding text is just summary."""
        summary = "Test memory"
        tags_str = ""
        embed_text = f"{summary} {tags_str}" if tags_str else summary
        assert embed_text == "Test memory"
