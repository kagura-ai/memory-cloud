"""Tests for cross-context recall filter construction.

Issue #81: Verify _build_search_filter handles single and multiple context IDs.
"""

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from db.qdrant import _build_search_filter


class TestBuildSearchFilterSingleContext:
    """Verify existing single-context behavior is preserved."""

    def test_single_context_uses_match_value(self):
        """Single context_id should use MatchValue (exact match)."""
        result = _build_search_filter(
            workspace_id="ws-1",
            context_id="ctx-1",
            user_id="user-1",
        )
        assert isinstance(result, Filter)
        context_conditions = [
            c for c in result.must if isinstance(c, FieldCondition) and c.key == "context_id"
        ]
        assert len(context_conditions) == 1
        assert isinstance(context_conditions[0].match, MatchValue)
        assert context_conditions[0].match.value == "ctx-1"

    def test_single_context_includes_user_id(self):
        """Private context should include user_id filter."""
        result = _build_search_filter(
            workspace_id="ws-1",
            context_id="ctx-1",
            user_id="user-1",
        )
        user_conditions = [
            c for c in result.must if isinstance(c, FieldCondition) and c.key == "user_id"
        ]
        assert len(user_conditions) == 1

    def test_shared_context_skips_user_id(self):
        """Shared context should skip user_id filter."""
        result = _build_search_filter(
            workspace_id="ws-1",
            context_id="ctx-1",
            user_id="user-1",
            is_shared_context=True,
        )
        user_conditions = [
            c for c in result.must if isinstance(c, FieldCondition) and c.key == "user_id"
        ]
        assert len(user_conditions) == 0


class TestBuildSearchFilterMultiContext:
    """Test cross-context recall with multiple context IDs (Issue #81)."""

    def test_multi_context_uses_match_any(self):
        """List of context_ids should use MatchAny."""
        result = _build_search_filter(
            workspace_id="ws-1",
            context_id=["ctx-1", "ctx-2", "ctx-3"],
            user_id="user-1",
        )
        assert isinstance(result, Filter)
        context_conditions = [
            c for c in result.must if isinstance(c, FieldCondition) and c.key == "context_id"
        ]
        assert len(context_conditions) == 1
        assert isinstance(context_conditions[0].match, MatchAny)
        assert context_conditions[0].match.any == ["ctx-1", "ctx-2", "ctx-3"]

    def test_multi_context_includes_user_id(self):
        """Cross-context should always include user_id filter."""
        result = _build_search_filter(
            workspace_id="ws-1",
            context_id=["ctx-1", "ctx-2"],
            user_id="user-1",
        )
        user_conditions = [
            c for c in result.must if isinstance(c, FieldCondition) and c.key == "user_id"
        ]
        assert len(user_conditions) == 1

    def test_multi_context_includes_workspace_id(self):
        """Cross-context should include workspace_id filter."""
        result = _build_search_filter(
            workspace_id="ws-1",
            context_id=["ctx-1", "ctx-2"],
            user_id="user-1",
        )
        ws_conditions = [
            c for c in result.must if isinstance(c, FieldCondition) and c.key == "workspace_id"
        ]
        assert len(ws_conditions) == 1
        assert ws_conditions[0].match.value == "ws-1"

    def test_multi_context_with_filters(self):
        """Cross-context should combine with metadata filters."""
        result = _build_search_filter(
            workspace_id="ws-1",
            context_id=["ctx-1", "ctx-2"],
            user_id="user-1",
            filters={"type": "code", "tags": ["python"]},
        )
        # Should have: workspace_id, context_id (MatchAny), user_id, type, tags
        assert len(result.must) == 5

    def test_two_contexts_minimum(self):
        """Two context IDs should work (minimum for cross-context)."""
        result = _build_search_filter(
            workspace_id="ws-1",
            context_id=["ctx-1", "ctx-2"],
            user_id="user-1",
        )
        context_conditions = [
            c for c in result.must if isinstance(c, FieldCondition) and c.key == "context_id"
        ]
        assert isinstance(context_conditions[0].match, MatchAny)
        assert len(context_conditions[0].match.any) == 2
