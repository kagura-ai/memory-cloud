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

pytestmark = pytest.mark.usefixtures("no_identity_links")


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


# #2957: a self-hosted embedding server that cannot be reached raises
# ``SelfHostedUnreachableError`` (a ``ConfigurationError`` subclass, CFG-001)
# from the reachability probe. That is a transient outage, like a provider
# that answers 5xx, so a hybrid recall serves its keyword arm instead of 500.
# A server that answered and refused (401/403) stays a plain
# ``ConfigurationError`` and keeps failing loudly.
_UNREACHABLE_BASE_URL = "http://inference.example.com:8000"


def _unreachable() -> Exception:
    from utils.exceptions import SelfHostedUnreachableError

    return SelfHostedUnreachableError(
        f"Cannot connect to self-hosted inference server at {_UNREACHABLE_BASE_URL}. "
        "Is your inference server running?"
    )


@pytest.mark.asyncio
class TestSelfHostedUnreachableDegrades:
    async def test_hybrid_unreachable_self_hosted_returns_keyword_hits_flagged_embedding_unavailable(
        self,
    ):
        degradation: dict = {}
        results, _ = await _search(
            search_mode="hybrid",
            embed_error=_unreachable(),
            keyword_hits=[{"id": "m1", "score": 0.9, "payload": {}}],
            degradation=degradation,
        )
        assert [r["id"] for r in results] == ["m1"]
        assert degradation["degraded"] is True
        assert degradation["reason"] == "embedding_unavailable"

    async def test_semantic_unreachable_self_hosted_still_raises(self):
        from utils.exceptions import SelfHostedUnreachableError

        with pytest.raises(SelfHostedUnreachableError) as excinfo:
            await _search(
                search_mode="semantic",
                embed_error=_unreachable(),
                keyword_hits=[{"id": "m1", "score": 0.9, "payload": {}}],
            )
        assert excinfo.value.status_code == 500
        assert excinfo.value.error_code == "CFG-001"

    async def test_hybrid_rejected_self_hosted_request_still_raises_cfg_001(self):
        # The probe's 401/403 answer: the server is up and refuses the key.
        # A misconfiguration, so it keeps failing loudly instead of degrading.
        from utils.exceptions import ConfigurationError, SelfHostedUnreachableError

        rejected = ConfigurationError(
            f"Self-hosted inference server at {_UNREACHABLE_BASE_URL} rejected the request "
            "(HTTP 401) — set or check SELF_HOSTED_API_KEY"
        )
        degradation: dict = {}
        with pytest.raises(ConfigurationError) as excinfo:
            await _search(
                search_mode="hybrid",
                embed_error=rejected,
                keyword_hits=[{"id": "m1", "score": 0.9, "payload": {}}],
                degradation=degradation,
            )
        assert not isinstance(excinfo.value, SelfHostedUnreachableError)
        assert excinfo.value.error_code == "CFG-001"
        assert degradation == {}

    async def test_unreachable_degrade_log_and_detail_carry_no_base_url(self):
        import structlog

        degradation: dict = {}
        with structlog.testing.capture_logs() as logs:
            await _search(
                search_mode="hybrid",
                embed_error=_unreachable(),
                keyword_hits=[],
                degradation=degradation,
            )
        (event,) = [e for e in logs if e["event"] == "recall_degraded_to_keyword"]
        assert event["log_level"] == "warning"
        assert event["reason"] == "embedding_unavailable"
        assert event["error_class"] == "SelfHostedUnreachableError"
        # The exception text names the configured base URL: only the class is
        # logged, and the detail is a fixed text.
        assert "error" not in event
        assert sorted(event) == ["context_id", "error_class", "event", "log_level", "reason"]
        assert degradation["detail"] == "self-hosted inference server unreachable"

    async def test_provider_failure_keeps_its_error_text_in_the_degrade_log(self):
        # The causes caught before #2957 keep their existing log fields.
        import structlog

        err = OpenAIError("Embedding generation failed: upstream down")
        degradation: dict = {}
        with structlog.testing.capture_logs() as logs:
            await _search(
                search_mode="hybrid",
                embed_error=err,
                keyword_hits=[],
                degradation=degradation,
            )
        (event,) = [e for e in logs if e["event"] == "recall_degraded_to_keyword"]
        assert event["reason"] == "embedding_unavailable"
        assert event["error"] == str(err)
        assert degradation["detail"] == str(err)
