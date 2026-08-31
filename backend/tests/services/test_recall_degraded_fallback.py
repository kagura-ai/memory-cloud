"""Recall degrades to keyword-only when the embedding provider fails (#1515).

Before this, any provider failure during ``recall`` surfaced as a 502: the
query embedding raises ``OpenAIError`` (an ``ExternalServiceError``, which
carries ``status_code=502``) and nothing caught it. But the default search
mode is *hybrid*, whose BM25 arm needs no embedding at all — so the request
can still be answered, just less well.

These tests pin that behaviour and, just as importantly, that the caller is
told: a silent downgrade would hand back keyword hits whose confidence is
computed on a different basis, with no way to tell.

An explicitly requested ``semantic`` search still raises. There is no keyword
arm to fall back to there, and returning BM25 hits to a caller who asked for
vector search answers a different question than the one asked.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from utils.exceptions import OpenAIError, QdrantError


def _fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        context_id=uuid4(),
        fetch_factor=2,
        use_rerank=False,
        semantic_weight=0.6,
        bm25_weight=0.4,
    )


def _embed_svc_that(*, raises: Exception | None = None) -> MagicMock:
    svc = MagicMock()
    svc.provider = "openai"
    svc.model = "text-embedding-3-small"
    if raises is not None:
        svc.embed_with_usage = AsyncMock(side_effect=raises)
    else:
        svc.embed_with_usage = AsyncMock(return_value=([0.1] * 8, 0))
    svc.resolve_paid_by = AsyncMock(return_value="platform")
    return svc


async def _search(
    *,
    search_mode: str,
    embed_error: Exception | None,
    keyword_hits: list[dict] | None = None,
    degradation: dict | None = None,
):
    """Run hybrid_search with every IO boundary mocked (same shape as
    test_recall_llm_instrumentation._run_hybrid_search)."""
    from services.search_service import SearchService

    embed_svc = _embed_svc_that(raises=embed_error)
    hits = keyword_hits if keyword_hits is not None else []

    with (
        patch("services.search_service.LLMCallLogWriter", MagicMock()),
        patch(
            "services.search_service.resolve_routing_from_config",
            return_value=("default", embed_svc),
        ),
        patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "services.search_service.search_memories_fulltext",
            new=AsyncMock(return_value=hits),
        ),
        patch(
            "services.context_service.ContextService.is_context_shared",
            new=AsyncMock(return_value=False),
        ),
    ):
        service = SearchService(db=AsyncMock())
        service._get_search_config = AsyncMock(return_value=_fake_config())

        results = await service.hybrid_search(
            query="test query",
            user_id="u-test",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
            k=5,
            use_rerank=False,
            filters=None,
            search_mode=search_mode,
            degradation=degradation,
        )
    return results, embed_svc


@pytest.mark.asyncio
class TestHybridDegradesToKeyword:
    async def test_embedding_failure_returns_keyword_hits_instead_of_raising(self):
        hits = [{"id": "m1", "score": 0.9, "payload": {}}]
        results, _ = await _search(
            search_mode="hybrid",
            embed_error=OpenAIError("Embedding generation failed: upstream down"),
            keyword_hits=hits,
        )
        assert [r["id"] for r in results] == ["m1"]

    async def test_the_caller_is_told_the_result_is_degraded(self):
        degradation: dict = {}
        await _search(
            search_mode="hybrid",
            embed_error=OpenAIError("Embedding generation failed: upstream down"),
            keyword_hits=[{"id": "m1", "score": 0.9, "payload": {}}],
            degradation=degradation,
        )
        assert degradation["degraded"] is True
        assert degradation["reason"] == "embedding_unavailable"

    async def test_a_vector_store_failure_degrades_the_same_way(self):
        # The Qdrant arm carries the same 502-shaped failure class as the
        # provider, so it must not be the one path that still hard-fails.
        degradation: dict = {}
        from services.search_service import SearchService

        embed_svc = _embed_svc_that()
        with (
            patch("services.search_service.LLMCallLogWriter", MagicMock()),
            patch(
                "services.search_service.resolve_routing_from_config",
                return_value=("default", embed_svc),
            ),
            patch(
                "services.search_service.search_memories_qdrant",
                new=AsyncMock(side_effect=QdrantError("vector store unreachable")),
            ),
            patch(
                "services.search_service.search_memories_fulltext",
                new=AsyncMock(return_value=[{"id": "m2", "score": 0.5, "payload": {}}]),
            ),
            patch(
                "services.context_service.ContextService.is_context_shared",
                new=AsyncMock(return_value=False),
            ),
        ):
            service = SearchService(db=AsyncMock())
            service._get_search_config = AsyncMock(return_value=_fake_config())
            results = await service.hybrid_search(
                query="q",
                user_id="u",
                workspace_id=str(uuid4()),
                context_id=str(uuid4()),
                k=5,
                search_mode="hybrid",
                degradation=degradation,
            )
        assert [r["id"] for r in results] == ["m2"]
        assert degradation["degraded"] is True

    async def test_a_healthy_hybrid_search_is_not_flagged(self):
        degradation: dict = {}
        await _search(
            search_mode="hybrid",
            embed_error=None,
            keyword_hits=[{"id": "m1", "score": 0.9, "payload": {}}],
            degradation=degradation,
        )
        assert degradation == {}

    async def test_degradation_is_optional_so_existing_callers_are_unaffected(self):
        # Omitting the out-param must not turn the fallback into a crash.
        results, _ = await _search(
            search_mode="hybrid",
            embed_error=OpenAIError("down"),
            keyword_hits=[{"id": "m1", "score": 0.9, "payload": {}}],
        )
        assert [r["id"] for r in results] == ["m1"]


@pytest.mark.asyncio
class TestSemanticStillRaises:
    async def test_explicit_semantic_search_does_not_silently_become_keyword(self):
        with pytest.raises(OpenAIError):
            await _search(
                search_mode="semantic",
                embed_error=OpenAIError("Embedding generation failed: upstream down"),
                keyword_hits=[{"id": "m1", "score": 0.9, "payload": {}}],
            )

    async def test_keyword_mode_never_touches_the_embedding_provider(self):
        degradation: dict = {}
        results, embed_svc = await _search(
            search_mode="keyword",
            embed_error=OpenAIError("would explode if called"),
            keyword_hits=[{"id": "m1", "score": 0.9, "payload": {}}],
            degradation=degradation,
        )
        embed_svc.embed_with_usage.assert_not_called()
        assert [r["id"] for r in results] == ["m1"]
        assert degradation == {}
