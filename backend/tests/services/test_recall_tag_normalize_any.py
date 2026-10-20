"""recall with the default tags_match='any' + tags_normalize keeps every requested tag (#2096).

recall widens a tags_normalize filter to every stored spelling of each requested
tag. The store builders cut the flat list to 50 entries, so with more than
about 16 requested tags that each gained spellings the later requested tags
were dropped and tolerance matched fewer memories than the exact filter. recall
now passes one group per requested tag under 'any' too, and the builders cap
groups, not spellings. These tests run the real ``MemoryService.recall`` with
only its IO mocked, take the filters it hands to ``hybrid_search``, and let
Qdrant's local mode (and the LanceDB clause builder) judge them.
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
USER = "u1"

# 20 requested tags, each stored in two more spellings: 60 widened entries.
REQUESTED = [f"topic-{n}" for n in range(20)]
VOCABULARY = {spelling: 1 for t in REQUESTED for spelling in (t, t.upper(), t.replace("-", "_"))}
TAGGED = {
    1: ["topic-19"],  # the last requested tag, as asked
    2: ["TOPIC-19"],  # ... in a stored spelling only
    3: ["topic_0"],
    4: ["unrelated"],
}


async def _filters_hybrid_search_receives(
    filters: dict, *, vocabulary: dict[str, int] = VOCABULARY
) -> dict:
    # A bare MagicMock session skips the #1941 capacity reads (tests/conftest.py).
    service = MemoryService(MagicMock())
    service.search_service.hybrid_search = AsyncMock(return_value=[])
    read = AsyncMock(return_value=vocabulary)
    with (
        patch("services.tag_resolution.fetch_vocabulary_cached", new=read),
        patch("services.tag_resolution.fetch_vocabulary", new=read),
    ):
        await service.recall(
            request=RecallRequest(query="q", k=5, filters=filters),
            user_id=USER,
            current_context_id=CTX,
            current_workspace_id=WS,
        )
    service.search_service.hybrid_search.assert_awaited_once()
    return service.search_service.hybrid_search.await_args.kwargs["filters"]


def _matching_ids(filters: dict, tagged: dict[int, list[str]] = TAGGED) -> set[int]:
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
            for i, tags in tagged.items()
        ],
    )
    qdrant_filter = _build_search_filter(str(WS), str(CTX), USER, filters=filters)
    hits, _ = client.scroll("t", scroll_filter=qdrant_filter, limit=100)
    return {int(h.id) for h in hits}


@pytest.mark.asyncio
class TestRecallTagsAnyWithNormalize:
    async def test_every_requested_tag_still_matches(self):
        filters = await _filters_hybrid_search_receives({"tags": REQUESTED, "tags_normalize": True})
        assert len(filters["tags"]) == 60, "the filter was not widened"
        assert _matching_ids(filters) == {1, 2, 3}

    async def test_the_lance_clause_keeps_every_spelling(self):
        filters = await _filters_hybrid_search_receives(
            {"tags": REQUESTED, "tags_match": "any", "tags_normalize": True}
        )
        clause = _build_tag_clause(filters)
        for spelling in VOCABULARY:
            assert f"array_has(tags, '{spelling}')" in clause
        assert " AND " not in clause

    async def test_normalize_never_returns_fewer_than_exact(self):
        exact = await _filters_hybrid_search_receives({"tags": REQUESTED})
        widened = await _filters_hybrid_search_receives({**exact, "tags_normalize": True})
        assert _matching_ids(exact) == {1}
        assert _matching_ids(exact) <= _matching_ids(widened)

    async def test_the_cap_counts_requested_tags(self):
        requested = [f"topic-{n}" for n in range(60)]
        vocabulary = {s: 1 for t in requested for s in (t, t.upper())}
        filters = await _filters_hybrid_search_receives(
            {"tags": requested, "tags_normalize": True}, vocabulary=vocabulary
        )
        tagged = {1: ["TOPIC-49"], 2: ["topic-50"], 3: ["TOPIC-50"]}
        # The exact filter also keeps the first 50 requested tags.
        assert _matching_ids({"tags": requested}, tagged) == set()
        assert _matching_ids(filters, tagged) == {1}
        assert "TOPIC-50" not in filters["tags"], "a tag past the cap was widened"

    async def test_an_unwidened_filter_carries_no_groups(self):
        filters = await _filters_hybrid_search_receives(
            {"tags": REQUESTED, "tags_normalize": True}, vocabulary={}
        )
        assert filters["tags"] == REQUESTED
        assert TAG_GROUPS_FILTER_KEY not in filters

    async def test_without_tags_normalize_nothing_changes(self):
        filters = await _filters_hybrid_search_receives({"tags": REQUESTED})
        assert filters["tags"] == REQUESTED
        assert TAG_GROUPS_FILTER_KEY not in filters
