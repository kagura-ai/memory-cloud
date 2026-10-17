"""recall with tags_match='all' + tags_normalize matches every spelling of each tag (#2058).

recall widens a tags_normalize filter to every stored spelling of each requested
tag and passes the flat list on with tags_match unchanged. Under 'all' the
builders used to AND that list, so every spelling became a required tag and the
memories tagged exactly as asked dropped out. These tests run the real
``MemoryService.recall`` with only its IO mocked, take the filters it hands to
``hybrid_search``, and let Qdrant's local mode judge which memories they match.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from db.qdrant import _build_search_filter
from models.schemas import RecallRequest
from services.memory_service import MemoryService

WS = uuid4()
CTX = uuid4()
USER = "u1"

# The issue's context: two memories tagged as asked, one tagged with the
# hiragana spelling only, one with a different tag set.
TAGGED = {
    1: ["ログ", "デプロイ"],
    2: ["ログ", "デプロイ", "メモ"],
    3: ["ろぐ"],
    4: ["デプロイ"],
}
VOCABULARY = {"ログ": 2, "デプロイ": 3, "ろぐ": 1, "メモ": 1}


async def _filters_hybrid_search_receives(filters: dict) -> dict:
    # A bare MagicMock session skips the #1941 capacity reads (tests/conftest.py).
    service = MemoryService(MagicMock())
    service.search_service.hybrid_search = AsyncMock(return_value=[])
    with (
        patch(
            "services.tag_resolution.fetch_vocabulary_cached",
            new=AsyncMock(return_value=VOCABULARY),
        ),
        patch("services.tag_resolution.fetch_vocabulary", new=AsyncMock(return_value=VOCABULARY)),
    ):
        await service.recall(
            request=RecallRequest(query="q", k=5, filters=filters),
            user_id=USER,
            current_context_id=CTX,
            current_workspace_id=WS,
        )
    service.search_service.hybrid_search.assert_awaited_once()
    return service.search_service.hybrid_search.await_args.kwargs["filters"]


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
