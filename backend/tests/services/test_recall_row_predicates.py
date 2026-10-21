"""The recall row filters as SQL, for a row read outside the search (#2027).

``recall_row_predicates`` projects a recall's effective filters onto the
vocabulary ``compile_memory_filters`` understands, so a supersede suggestion's
target is held to the same row filters as the rows themselves. Each case is
compared against ``compile_memory_filters`` of the equivalent ``list`` filter:
the adapter is a projection, not a second implementation.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import cast
from sqlalchemy.dialects import postgresql

from models.memory import Memory
from services.memory_listing import compile_memory_filters, recall_row_predicates
from utils.tag_normalize import TAG_GROUPS_FILTER_KEY, tag_filter_groups, tag_filter_groups_any


def _sql(predicates) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for predicate in predicates:
        compiled = predicate.compile(dialect=postgresql.dialect())
        out.append((str(compiled), dict(compiled.params)))
    return out


def _same(actual, expected) -> None:
    assert _sql(actual) == _sql(expected)


class TestProjection:
    def test_projects_the_five_row_filters_and_scope(self):
        filters = {
            "type": "decision",
            "scope": "persistent",
            "source_type": "manual",
            "source_uri_prefix": "docs/",
            "importance": {"gte": 0.5, "lt": 0.9},
            "tags": ["infra", "db"],
            "tags_match": "all",
        }

        _same(recall_row_predicates(filters), compile_memory_filters(filters))

    def test_none_and_empty_are_no_predicates(self):
        assert recall_row_predicates(None) == []
        assert recall_row_predicates({}) == []

    def test_ignores_query_shape_and_scope_keys(self):
        filters = {
            "near": {"lat": 35.0, "lon": 139.0, "radius_km": 5},
            "within": {"bbox": [0, 0, 1, 1]},
            "analysis_cluster": "c1",
            "client": "claude-code",
            "score_threshold": 0.4,
            "trust_tier": "trusted",
            TAG_GROUPS_FILTER_KEY: [["a"]],
            "something_new": 1,
        }

        assert recall_row_predicates(filters) == []

    def test_a_non_string_single_value_filter_is_ignored(self):
        # recall's Qdrant builder matches one value; a list never got that far.
        assert recall_row_predicates({"type": ["a", "b"], "scope": 3, "source_type": None}) == []
        assert recall_row_predicates({"source_uri_prefix": ""}) == []
        # Hydration skips an empty source_type (``if stype``): unset, not "".
        assert recall_row_predicates({"source_type": ""}) == []

    def test_importance_takes_known_ops_only_and_ignores_a_non_dict(self):
        _same(
            recall_row_predicates({"importance": {"gte": 0.5, "eq": 0.7, "lt": None}}),
            compile_memory_filters({"importance": {"gte": 0.5}}),
        )
        assert recall_row_predicates({"importance": 0.5}) == []
        assert recall_row_predicates({"importance": {}}) == []
        assert recall_row_predicates({"importance": {"gte": "high"}}) == []

    def test_importance_bounds_are_read_as_recalls_range_reads_them(self):
        # Qdrant's Range coerces a numeric string and a bool; so does this.
        _same(
            recall_row_predicates({"importance": {"gte": "0.7", "lte": True}}),
            compile_memory_filters({"importance": {"gte": 0.7, "lte": 1.0}}),
        )

    def test_tags_drop_non_strings_and_cap_at_fifty(self):
        tags = [1, "", None, *(f"t{i}" for i in range(60))]

        _same(
            recall_row_predicates({"tags": tags}),
            compile_memory_filters({"tags": [f"t{i}" for i in range(50)]}),
        )

    def test_tags_that_are_not_a_list_are_ignored(self):
        # recall's tag builder ignores a bare string; so does the projection.
        assert recall_row_predicates({"tags": "infra"}) == []
        assert recall_row_predicates({"tags": [1, None]}) == []

    def test_tags_normalize_is_not_applied_again(self):
        # The widening already happened in recall(); list's SQL folding is a
        # different mechanism and must not run on top of it.
        _same(
            recall_row_predicates({"tags": ["Infra"], "tags_normalize": True}),
            compile_memory_filters({"tags": ["Infra"]}),
        )

    def test_time_windows_are_not_applied(self):
        # Decision B (#2027): a target is older than its source by construction.
        filters = {
            "created_after": "2026-01-01T00:00:00Z",
            "created_before": "2026-12-01T00:00:00Z",
            "updated_after": "2026-01-01T00:00:00Z",
            "updated_before": "2026-12-01T00:00:00Z",
        }

        assert recall_row_predicates(filters) == []


class TestWidenedTags:
    def test_widened_groups_all_is_one_overlap_per_group(self):
        requested = ["deploy", "db"]
        expanded = ["deploy", "db", "Deploy", "DB"]
        filters = {
            "tags": expanded,
            "tags_match": "all",
            "tags_normalize": True,
            TAG_GROUPS_FILTER_KEY: tag_filter_groups(requested, expanded),
        }

        expected = [
            Memory.tags.overlap(cast(["deploy", "Deploy"], Memory.tags.type)),
            Memory.tags.overlap(cast(["db", "DB"], Memory.tags.type)),
        ]
        _same(recall_row_predicates(filters), expected)

    def test_widened_groups_any_is_the_union(self):
        requested = ["deploy", "db"]
        added = {"deploy": ["Deploy"], "db": ["DB"]}
        filters = {
            "tags": ["deploy", "Deploy", "db", "DB"],
            "tags_match": "any",
            "tags_normalize": True,
            TAG_GROUPS_FILTER_KEY: tag_filter_groups_any(requested, added),
        }

        _same(
            recall_row_predicates(filters),
            compile_memory_filters({"tags": ["deploy", "Deploy", "db", "DB"]}),
        )

    def test_an_unknown_tags_match_is_ignored(self):
        # recall's builder refuses it, so the recall never reaches the page.
        assert recall_row_predicates({"tags": ["a"], "tags_match": "some"}) == []


@pytest.mark.parametrize(
    "filters",
    [
        {"near": {"lat": 1}},
        {"importance": "high"},
        {"tags_match": "all"},
        {TAG_GROUPS_FILTER_KEY: [["a"]]},
    ],
)
def test_the_projection_never_raises_where_compile_does(filters):
    with pytest.raises(ValueError):
        compile_memory_filters(filters)

    assert recall_row_predicates(filters) == []
