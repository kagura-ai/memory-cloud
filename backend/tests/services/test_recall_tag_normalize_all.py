"""recall with tags_match='all' + tags_normalize matches every spelling of each tag (#2058).

recall widens a tags_normalize filter to every stored spelling of each requested
tag and passes the flat list on with tags_match unchanged. Under 'all' the
builders used to AND that list, so every spelling became a required tag and the
memories tagged exactly as asked dropped out. recall now passes one group per
requested tag; two requested tags with one fold (``v0.11.0`` / ``v0.1.10``) stay
two required tags, widened or not (#2071). These tests run the real
``MemoryService.recall`` with only its IO mocked, take the filters it hands to
``hybrid_search``, and let Qdrant's local mode judge which memories they match.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from db.lance_store import _build_tag_clause
from db.qdrant import _build_search_filter
from models.schemas import RecallRequest
from services.memory_service import MemoryService
from utils.tag_normalize import TAG_GROUPS_FILTER_KEY

WS = uuid4()
CTX = uuid4()
CTX2 = uuid4()
USER = "u1"

# The issue's context: two memories tagged as asked, one tagged with the
# hiragana spelling only, one with a different tag set.
TAGGED = {
    1: ["ログ", "デプロイ"],
    2: ["ログ", "デプロイ", "メモ"],
    3: ["ろぐ"],
    4: ["デプロイ"],
    # #2071: two version tags that share the normalize_tag fold "v0110",
    # beside a tag whose stored spelling ("Release") differs from a request.
    5: ["v0.11.0"],
    6: ["v0.1.10"],
    7: ["v0.11.0", "v0.1.10"],
    8: ["v0.11.0", "Release"],
    9: ["v0.11.0", "v0.1.10", "Release"],
    10: ["V0.11.0"],
    11: ["deploy"],
    12: ["Deploy"],
}
VOCABULARY = {"ログ": 2, "デプロイ": 3, "ろぐ": 1, "メモ": 1}
VERSIONS = ["v0.11.0", "v0.1.10"]


async def _filters_hybrid_search_receives(
    filters: dict,
    *,
    vocabulary: dict[str, int] | Exception = VOCABULARY,
    context_ids: list | None = None,
    seen: dict | None = None,
) -> dict:
    # A bare MagicMock session skips the #1941 capacity reads (tests/conftest.py).
    service = MemoryService(MagicMock())
    service.search_service.hybrid_search = AsyncMock(return_value=[])
    read = (
        AsyncMock(side_effect=vocabulary)
        if isinstance(vocabulary, Exception)
        else AsyncMock(return_value=vocabulary)
    )
    with (
        patch("services.tag_resolution.fetch_vocabulary_cached", new=read),
        patch("services.tag_resolution.fetch_vocabulary", new=read),
    ):
        await service.recall(
            request=RecallRequest(query="q", k=5, filters=filters),
            user_id=USER,
            current_context_id=CTX,
            current_workspace_id=WS,
            context_ids=context_ids,
        )
    service.search_service.hybrid_search.assert_awaited_once()
    kwargs = service.search_service.hybrid_search.await_args.kwargs
    if seen is not None:
        seen.update(kwargs, vocabulary_reads=read.await_count)
    return kwargs["filters"]


def _matching_ids(filters: dict) -> set[int]:
    """Ids the store returns for these filters, judged by Qdrant's local mode."""
    client = QdrantClient(":memory:")
    client.create_collection("t", vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    client.upsert(
        "t",
        points=[
            PointStruct(
                id=i,
                vector=[1.0, 0.0],
                payload={
                    "workspace_id": str(WS),
                    "context_id": str(CTX),
                    "user_id": USER,
                    "tags": tags,
                },
            )
            for i, tags in TAGGED.items()
        ],
    )
    qdrant_filter = _build_search_filter(str(WS), str(CTX), USER, filters=filters)
    hits, _ = client.scroll("t", scroll_filter=qdrant_filter, limit=100)
    return {int(h.id) for h in hits}


@pytest.mark.asyncio
class TestRecallTagsAllWithNormalize:
    async def test_the_exactly_tagged_memories_still_match(self):
        filters = await _filters_hybrid_search_receives(
            {"tags": ["ログ", "デプロイ"], "tags_match": "all", "tags_normalize": True}
        )
        assert "ろぐ" in filters["tags"], "the filter was not widened to the stored spelling"
        assert _matching_ids(filters) == {1, 2}

    async def test_normalize_never_returns_fewer_than_exact(self):
        exact = await _filters_hybrid_search_receives(
            {"tags": ["ログ", "デプロイ"], "tags_match": "all"}
        )
        widened = await _filters_hybrid_search_receives({**exact, "tags_normalize": True})
        assert _matching_ids(exact) == {1, 2}
        assert _matching_ids(exact) <= _matching_ids(widened)

    async def test_a_hiragana_request_matches_the_katakana_memories(self):
        filters = await _filters_hybrid_search_receives(
            {"tags": ["ろぐ", "デプロイ"], "tags_match": "all", "tags_normalize": True}
        )
        assert _matching_ids(filters) == {1, 2}

    async def test_any_is_unchanged(self):
        filters = await _filters_hybrid_search_receives(
            {"tags": ["ログ"], "tags_match": "any", "tags_normalize": True}
        )
        assert _matching_ids(filters) == {1, 2, 3}


@pytest.mark.asyncio
class TestSameFoldRequestedTags:
    """#2071: two requested tags with one fold are two required tags."""

    ALL = {"tags": VERSIONS, "tags_match": "all", "tags_normalize": True}
    EXACT_CLAUSE = "(array_has(tags, 'v0.11.0') AND array_has(tags, 'v0.1.10'))"

    def _assert_both_required(self, filters: dict) -> None:
        assert _matching_ids(filters) == {7, 9}  # the memories carrying both
        assert _build_tag_clause(filters) == self.EXACT_CLAUSE

    async def test_multi_context_recall_requires_both(self):
        both = {"v0.11.0": 2, "v0.1.10": 2}
        seen: dict = {}
        filters = await _filters_hybrid_search_receives(
            self.ALL, vocabulary=both, context_ids=[CTX, CTX2], seen=seen
        )
        assert seen["context_id"] == [str(CTX), str(CTX2)]
        assert seen["vocabulary_reads"] == 0, "one context's vocabulary widened many"
        assert filters["tags"] == VERSIONS
        assert TAG_GROUPS_FILTER_KEY not in filters
        self._assert_both_required(filters)

    async def test_a_failed_vocabulary_read_requires_both(self):
        filters = await _filters_hybrid_search_receives(
            self.ALL, vocabulary=RuntimeError("vocabulary down")
        )
        self._assert_both_required(filters)

    async def test_no_stored_variant_requires_both(self):
        # Only one spelling is stored, so neither tag gains a variant.
        filters = await _filters_hybrid_search_receives(self.ALL, vocabulary={"v0.11.0": 2})
        self._assert_both_required(filters)

    async def test_an_empty_vocabulary_requires_both(self):
        filters = await _filters_hybrid_search_receives(self.ALL, vocabulary={})
        self._assert_both_required(filters)

    async def test_both_spellings_stored_still_requires_both(self):
        # Each is a stored spelling of the other, but neither is the other's
        # variant: the caller listed both.
        both = {"v0.11.0": 2, "v0.1.10": 2}
        filters = await _filters_hybrid_search_receives(self.ALL, vocabulary=both)
        self._assert_both_required(filters)

    async def test_a_third_spelling_counts_for_one_of_them_only(self):
        filters = await _filters_hybrid_search_receives(
            self.ALL, vocabulary={"v0.11.0": 2, "v0.1.10": 2, "V0.11.0": 1}
        )
        assert _matching_ids(filters) == {7, 9}  # not 10, which has only V0.11.0
        assert _build_tag_clause(filters) == (
            "((array_has(tags, 'v0.11.0') OR array_has(tags, 'V0.11.0')) AND "
            "array_has(tags, 'v0.1.10'))"
        )

    async def test_widening_another_tag_does_not_merge_them(self):
        filters = await _filters_hybrid_search_receives(
            {**self.ALL, "tags": [*VERSIONS, "release"]},
            vocabulary={"v0.11.0": 2, "v0.1.10": 2, "Release": 2},
        )
        assert "Release" in filters["tags"], "the filter was not widened"
        assert _matching_ids(filters) == {9}
        assert _build_tag_clause(filters) == (
            "(array_has(tags, 'v0.11.0') AND array_has(tags, 'v0.1.10') AND "
            "(array_has(tags, 'release') OR array_has(tags, 'Release')))"
        )

    async def test_one_tag_listed_in_two_spellings_needs_either(self):
        # Nothing to add (both stored spellings are listed), yet they are one
        # tag written twice and still form one group.
        filters = await _filters_hybrid_search_receives(
            {"tags": ["Deploy", "deploy"], "tags_match": "all", "tags_normalize": True},
            vocabulary={"Deploy": 1, "deploy": 1},
        )
        assert _matching_ids(filters) == {11, 12}

    async def test_a_caller_cannot_supply_the_groups(self):
        filters = await _filters_hybrid_search_receives(
            {"tags": VERSIONS, "tags_match": "all", TAG_GROUPS_FILTER_KEY: [VERSIONS]}
        )
        assert TAG_GROUPS_FILTER_KEY not in filters
        self._assert_both_required(filters)
