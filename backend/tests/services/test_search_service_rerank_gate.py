"""#1572: the effective ``use_rerank`` flag and fail-open at the SearchService gate.

Before #1572 a context's ``use_rerank=true`` was inert — callers defaulted to
False and the gate is an AND. Now:

- ``use_rerank=None`` (caller omitted it) follows the context config;
- an explicit False forces reranking off; an explicit True still requires the
  context to allow it (#130, unchanged);
- ``ENABLE_RERANKING=false`` never reranks, whatever the flags say;
- the plan gate (``reranking`` feature) is unchanged: a free workspace does not
  rerank;
- a provider failure fails open to the un-reranked hybrid results and emits the
  stable ``rerank_failed_open`` event (the operator-alertable telemetry).

House style follows test_search_service.py: Qdrant/BM25/ContextService patched,
``_get_search_config`` replaced with a duck-typed config object.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import structlog

from config.settings import Settings
from services.reranker_service import RerankerService, VLLMReranker
from services.search_service import SearchService

_WS = "00000000-0000-0000-0000-000000000001"
_CTX = "00000000-0000-0000-0000-000000000002"

_RERANK_ENV = [
    "ENABLE_RERANKING",
    "RERANK_BASE_URL",
    "RERANK_MODEL",
    "SELF_HOSTED_BASE_URL",
    "DEFAULT_RERANKER_PROVIDER",
    "DEFAULT_USE_RERANK",
    "DEFAULT_RERANKER_MODEL",
]


def _config(use_rerank: bool, provider: str = "self_hosted", model: str | None = None):
    return type(
        "Cfg",
        (),
        {
            "semantic_weight": 0.6,
            "bm25_weight": 0.4,
            "fetch_factor": 3,
            "use_rerank": use_rerank,
            "reranker_provider": provider,
            "reranker_model": model,
            "embedding_model": "text-embedding-3-small",
            "embedding_dimensions": 512,
        },
    )()


@pytest.fixture(autouse=True)
def _routing():
    with patch(
        "services.search_service.resolve_routing_from_config",
        return_value=(
            "kagura_memories",
            MagicMock(
                embed_with_usage=AsyncMock(return_value=([0.1] * 512, 0)),
                provider="openai",
                model="text-embedding-3-small",
            ),
        ),
    ):
        yield


@pytest.fixture
def settings(monkeypatch) -> Settings:
    for key in _RERANK_ENV:
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)
    monkeypatch.setattr("services.search_service.get_settings", lambda: s)
    monkeypatch.setattr("config.settings.get_settings", lambda: s)
    return s


@pytest.fixture
def db():
    return MagicMock()


@pytest.fixture
def service(db):
    svc = SearchService(db)
    svc.reranker_service = MagicMock()
    svc.reranker_service.rerank = AsyncMock(
        return_value=[{"id": "m2", "score": 0.8, "payload": {"summary": "b"}, "rerank_score": 0.9}]
    )
    return svc


_SEMANTIC = [
    {"id": "m1", "score": 0.9, "payload": {"summary": "a"}},
    {"id": "m2", "score": 0.8, "payload": {"summary": "b"}},
]


async def _search(service, *, use_rerank, config, plan_allows=True, workspace_id=_WS):
    service._get_search_config = AsyncMock(return_value=config)
    ctx_svc = MagicMock()
    ctx_svc.is_context_shared = AsyncMock(return_value=False)
    quota = MagicMock()
    quota.check_feature_access = AsyncMock(
        return_value=(plan_allows, None if plan_allows else "reranking requires Basic")
    )
    with (
        patch(
            "services.search_service.search_memories_qdrant", new=AsyncMock(return_value=_SEMANTIC)
        ),
        patch("services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])),
        patch("services.context_service.ContextService", return_value=ctx_svc),
        patch("services.quota_service.QuotaService", return_value=quota),
    ):
        return await service.hybrid_search(
            query="q",
            user_id="u",
            workspace_id=workspace_id,
            context_id=_CTX,
            k=5,
            use_rerank=use_rerank,
        )


class TestEffectiveFlag:
    async def test_omitted_flag_follows_context_config_on(self, service, settings):
        await _search(service, use_rerank=None, config=_config(True))
        service.reranker_service.rerank.assert_awaited_once()

    async def test_omitted_flag_follows_context_config_off(self, service, settings):
        """Acceptance 4: existing contexts hold use_rerank=false → unchanged."""
        await _search(service, use_rerank=None, config=_config(False))
        service.reranker_service.rerank.assert_not_awaited()

    async def test_explicit_false_forces_off(self, service, settings):
        await _search(service, use_rerank=False, config=_config(True))
        service.reranker_service.rerank.assert_not_awaited()

    async def test_explicit_true_still_requires_the_context(self, service, settings):
        """#130 AND gate is unchanged for callers that pass a bool."""
        await _search(service, use_rerank=True, config=_config(False))
        service.reranker_service.rerank.assert_not_awaited()

    async def test_explicit_true_with_context_on_reranks(self, service, settings):
        await _search(service, use_rerank=True, config=_config(True))
        service.reranker_service.rerank.assert_awaited_once()

    async def test_reranker_gets_the_doubled_fetch_window(self, service, settings):
        """Issue #67: the fetch window doubles when reranking is effective, and
        that must key off the resolved flag, not the raw caller argument."""
        await _search(service, use_rerank=None, config=_config(True))
        kwargs = service.reranker_service.rerank.await_args.kwargs
        assert kwargs["context_id"] == _CTX and kwargs["workspace_id"] == _WS


class TestDeploymentAndPlanGates:
    async def test_enable_reranking_false_never_reranks(self, service, settings):
        """Acceptance 4: ENABLE_RERANKING=false wins even with both flags true."""
        settings.enable_reranking = False
        results = await _search(service, use_rerank=True, config=_config(True))
        service.reranker_service.rerank.assert_not_awaited()
        # Plain hybrid results still come back; the skip itself is logged at
        # debug (``reranking_disabled_by_deployment``), below the capture floor.
        assert [r["id"] for r in results] == ["m1", "m2"]

    async def test_free_workspace_does_not_rerank(self, service, settings):
        """Acceptance 1 (free half): the plan gate still decides."""
        with structlog.testing.capture_logs() as logs:
            await _search(service, use_rerank=None, config=_config(True), plan_allows=False)
        service.reranker_service.rerank.assert_not_awaited()
        assert any(e["event"] == "reranking_disabled_by_plan_tier" for e in logs)


class TestFailOpen:
    async def test_provider_failure_returns_hybrid_results_and_logs_stable_event(
        self, service, settings
    ):
        """Acceptance 3: reranker outage → un-reranked results + rerank_failed_open."""
        service.reranker_service.rerank = AsyncMock(
            side_effect=RuntimeError("self_hosted rerank backend unavailable")
        )
        with structlog.testing.capture_logs() as logs:
            results = await _search(service, use_rerank=None, config=_config(True, "self_hosted"))

        assert [r["id"] for r in results] == ["m1", "m2"], "hybrid order, never an empty recall"
        events = [e for e in logs if e["event"] == "rerank_failed_open"]
        assert len(events) == 1
        event = events[0]
        assert event["log_level"] == "warning"
        assert event["provider"] == "self_hosted"
        assert event["error_class"] == "RuntimeError"
        assert event["context_id"] == _CTX
        assert event["workspace_id"] == _WS

    async def test_http_error_from_vllm_fails_open_too(self, service, settings):
        service.reranker_service.rerank = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with structlog.testing.capture_logs() as logs:
            results = await _search(service, use_rerank=None, config=_config(True))
        assert len(results) == 2
        assert [e["error_class"] for e in logs if e["event"] == "rerank_failed_open"] == [
            "ConnectError"
        ]


class TestSelfHostedDefaultEndToEnd:
    """Acceptance 1 (recall half): a context stamped with the self_hosted
    deployment default reranks through RERANK_BASE_URL with use_rerank omitted
    and no ExternalAPIKey — the real RerankerService selects VLLMReranker."""

    async def test_omitted_flag_reranks_through_vllm_without_a_key(self, db, monkeypatch):
        for key in _RERANK_ENV:
            monkeypatch.delenv(key, raising=False)
        s = Settings(
            _env_file=None,
            rerank_base_url="http://gpu:8002",
            rerank_model="qwen3-reranker-0.6b",
            default_reranker_provider="self_hosted",
            default_use_rerank=True,
        )
        monkeypatch.setattr("services.search_service.get_settings", lambda: s)
        monkeypatch.setattr("config.settings.get_settings", lambda: s)

        # The stored row a fresh context gets under this deployment.
        ctx_row = MagicMock()
        ctx_row.use_rerank = True
        ctx_row.reranker_provider = "self_hosted"
        ctx_row.reranker_model = "qwen3-reranker-0.6b"
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: ctx_row))

        posted: list[tuple[str, dict]] = []

        async def fake_post(self, url, json=None, **kwargs):
            posted.append((url, json))
            resp = MagicMock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {
                "results": [
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.10},
                ]
            }
            return resp

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        service = SearchService(db)
        assert isinstance(service.reranker_service, RerankerService)
        chosen = await service.reranker_service.get_active_provider(
            "u", context_id=_CTX, workspace_id=_WS
        )
        assert isinstance(chosen, VLLMReranker)

        results = await _search(service, use_rerank=None, config=_config(True, "self_hosted"))

        assert len(posted) == 1 and posted[0][0] == "http://gpu:8002/v1/rerank"
        assert posted[0][1]["model"] == "qwen3-reranker-0.6b"
        assert [r["id"] for r in results] == ["m2", "m1"], "reranked order from /v1/rerank"
        assert results[0]["rerank_score"] == pytest.approx(0.95)
