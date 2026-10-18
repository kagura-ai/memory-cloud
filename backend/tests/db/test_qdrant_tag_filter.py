"""Tests for tag filtering and BM25 search integration.

Issue #67: Verify tags are properly indexed, filtered, and searched.
Issue #79: Verify AND logic for tag filters (tags_match="all").
"""

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from db.qdrant import _build_tag_filter_conditions
from utils.tag_normalize import TAG_GROUPS_FILTER_KEY


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

    def test_tags_match_invalid_value(self):
        """Invalid tags_match value should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="Invalid tags_match value"):
            _build_tag_filter_conditions({"tags": ["a"], "tags_match": "al"})

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


def _matching_ids(tagged: dict[int, list[str]], filters: dict) -> set[int]:
    """Ids whose tags satisfy the built conditions, judged by Qdrant itself.

    Local (in-process) mode applies the same filter semantics as the server, so
    this checks what the conditions MATCH, not only their shape.
    """
    client = QdrantClient(":memory:")
    client.create_collection("t", vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    client.upsert(
        "t",
        points=[
            PointStruct(id=i, vector=[1.0, 0.0], payload={"tags": tags})
            for i, tags in tagged.items()
        ],
    )
    hits, _ = client.scroll(
        "t", scroll_filter=Filter(must=_build_tag_filter_conditions(filters)), limit=100
    )
    return {int(h.id) for h in hits}


class TestTagFilterAllWithNormalize:
    """#2058: under tags_match='all', a widened filter ANDs one group per tag.

    recall widens the filter to every stored spelling of each requested tag.
    ANDing that flat list made every spelling a required tag, so turning
    tolerance on returned fewer memories, not more. recall now also passes the
    groups, and the builder ANDs those (#2071).
    """

    # recall's expansion of ["ログ", "デプロイ"] when another memory is tagged ろぐ.
    EXPANDED = {
        "tags": ["ログ", "デプロイ", "ろぐ"],
        "tags_match": "all",
        "tags_normalize": True,
        TAG_GROUPS_FILTER_KEY: [["ログ", "ろぐ"], ["デプロイ"]],
    }

    def test_spellings_of_one_tag_become_one_match_any(self):
        conditions = _build_tag_filter_conditions(self.EXPANDED)

        assert len(conditions) == 2
        assert isinstance(conditions[0].match, MatchAny)
        assert conditions[0].match.any == ["ログ", "ろぐ"]
        assert isinstance(conditions[1].match, MatchValue)
        assert conditions[1].match.value == "デプロイ"

    def test_without_variants_the_conditions_are_unchanged(self):
        """A tag with one spelling keeps the exact MatchValue of plain 'all'."""
        plain = {"tags": ["python", "fastapi"], "tags_match": "all"}
        grouped = {**plain, TAG_GROUPS_FILTER_KEY: [["python"], ["fastapi"]]}
        assert _build_tag_filter_conditions(grouped) == _build_tag_filter_conditions(plain)

    def test_without_groups_every_tag_stays_required(self):
        """#2071: tags_normalize alone does not regroup the list by fold.

        Without a widening the list is the request as given, and two listed tags
        with one fold (v0.11.0 / v0.1.10) are both required.
        """
        filters = {"tags": ["v0.11.0", "v0.1.10"], "tags_match": "all", "tags_normalize": True}
        conditions = _build_tag_filter_conditions(filters)
        assert [c.match.value for c in conditions] == ["v0.11.0", "v0.1.10"]

    def test_malformed_groups_are_skipped(self):
        filters = {
            **self.EXPANDED,
            TAG_GROUPS_FILTER_KEY: [["ログ", 3, ""], "デプロイ", [], [None], ["not-in-tags"]],
        }
        conditions = _build_tag_filter_conditions(filters)
        assert [c.match.value for c in conditions] == ["ログ"]

    def test_groups_that_are_all_malformed_fall_back_to_every_tag(self):
        filters = {**self.EXPANDED, TAG_GROUPS_FILTER_KEY: [[], "x"]}
        conditions = _build_tag_filter_conditions(filters)
        assert [c.match.value for c in conditions] == ["ログ", "デプロイ", "ろぐ"]

    def test_any_stays_one_flat_match_any(self):
        conditions = _build_tag_filter_conditions({**self.EXPANDED, "tags_match": "any"})
        assert len(conditions) == 1
        assert conditions[0].match.any == ["ログ", "デプロイ", "ろぐ"]

    def test_the_issue_scenario_matches_the_exactly_tagged_memories(self):
        tagged = {
            1: ["ログ", "デプロイ"],
            2: ["ろぐ", "デプロイ"],
            3: ["ろぐ"],
            4: ["デプロイ"],
        }
        assert _matching_ids(tagged, self.EXPANDED) == {1, 2}

    def test_normalize_never_matches_fewer_than_exact(self):
        """Tolerance only widens: every exact 'all' match stays matched."""
        tagged = {1: ["ログ", "デプロイ"], 2: ["ろぐ", "デプロイ"], 3: ["ログ"]}
        exact = _matching_ids(tagged, {"tags": ["ログ", "デプロイ"], "tags_match": "all"})
        assert exact == {1}
        assert exact <= _matching_ids(tagged, self.EXPANDED)

    def test_variants_cannot_push_a_requested_tag_past_the_cap(self):
        """The 50-tag cap counts tags, not spellings, under all + normalize."""
        groups = [[stem] + [f"{stem}{'-' * n}" for n in range(1, 21)] for stem in ("a", "b", "c")]
        groups.append(["delta"])  # entry 64 of the flat list: past a flat cap of 50
        widened = [t for g in groups for t in g]
        conditions = _build_tag_filter_conditions(
            {"tags": widened, "tags_match": "all", TAG_GROUPS_FILTER_KEY: groups}
        )
        assert len(conditions) == 4
        assert conditions[-1].match.value == "delta"
        assert len(conditions[0].match.any) == 21
