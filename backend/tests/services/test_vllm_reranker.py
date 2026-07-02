"""Unit tests for the OpenAI/Jina-style /v1/rerank path of the local reranker.

When ``settings.rerank_base_url`` is set, the local (context-config
"self_hosted") reranker path returns a ``VLLMReranker`` that makes ONE batched
POST to ``{base}/v1/rerank`` (vLLM ``--runner pooling`` seq-cls, TEI, or
Infinity) instead of SelfHostedReranker's per-document prompt scoring.

House style follows test_reranker_service_coverage.py: httpx.AsyncClient.post
is patched with canned responses (never a real network call); RerankerService
DB access goes through a MagicMock AsyncSession.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from services.reranker_service import (
    DEFAULT_VLLM_RERANK_MODEL,
    RerankerService,
    SelfHostedReranker,
    VLLMReranker,
)


def _result_scalar_one_or_none(value):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


class _FakeHttpResponse:
    def __init__(self, *, json_data=None, raise_exc=None):
        self._json_data = json_data
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._json_data


class TestVLLMReranker:
    """VLLMReranker: batched /v1/rerank call, sorting, validation, errors."""

    def test_init_defaults(self):
        r = VLLMReranker(base_url="http://gpu:8002/")
        assert r.provider_name == "vllm"
        assert r.base_url == "http://gpu:8002"  # trailing slash stripped
        assert r.model == DEFAULT_VLLM_RERANK_MODEL

    async def test_empty_documents_returns_empty(self):
        r = VLLMReranker(base_url="http://gpu:8002")
        assert await r.rerank("q", [], top_n=5) == []

    async def test_non_positive_top_n_raises_value_error(self):
        r = VLLMReranker(base_url="http://gpu:8002")
        with pytest.raises(ValueError):
            await r.rerank("q", ["doc"], top_n=0)

    async def test_posts_v1_rerank_once_and_sorts(self, monkeypatch):
        """One batched POST to {base}/v1/rerank; results sorted by score desc."""
        calls: list[tuple[str, dict]] = []

        async def fake_post(self, url, json=None, **kwargs):
            calls.append((url, json))
            return _FakeHttpResponse(
                json_data={
                    "results": [
                        {"index": 0, "relevance_score": 0.2},
                        {"index": 1, "relevance_score": 0.9},
                    ]
                }
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        r = VLLMReranker(base_url="http://gpu:8002", model="qwen3-reranker-0.6b")
        out = await r.rerank("q", ["a", "b"], top_n=2)

        assert len(calls) == 1  # batched: one HTTP call for all documents
        url, body = calls[0]
        assert url == "http://gpu:8002/v1/rerank"
        assert body == {
            "model": "qwen3-reranker-0.6b",
            "query": "q",
            "documents": ["a", "b"],
            "top_n": 2,
        }
        assert out[0] == {"index": 1, "relevance_score": 0.9}
        assert out[1] == {"index": 0, "relevance_score": 0.2}

    async def test_top_n_truncates_after_sort(self, monkeypatch):
        async def fake_post(self, url, json=None, **kwargs):
            return _FakeHttpResponse(
                json_data={
                    "results": [
                        {"index": 0, "relevance_score": 0.5},
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 2, "relevance_score": 0.1},
                    ]
                }
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        r = VLLMReranker(base_url="http://gpu:8002")
        out = await r.rerank("q", ["a", "b", "c"], top_n=1)
        assert out == [{"index": 1, "relevance_score": 0.9}]

    async def test_http_error_propagates(self, monkeypatch):
        exc = httpx.HTTPStatusError("boom", request=MagicMock(), response=MagicMock())

        async def fake_post(self, url, json=None, **kwargs):
            return _FakeHttpResponse(raise_exc=exc)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        r = VLLMReranker(base_url="http://gpu:8002")
        with pytest.raises(httpx.HTTPStatusError):
            await r.rerank("q", ["a"], top_n=1)

    async def test_malformed_response_raises_clear_value_error(self, monkeypatch):
        """A 200 body without a 'results' list fails with a clear ValueError.

        Guards against opaque KeyError/TypeError when the endpoint returns an
        unexpected shape (proxy error page, different schema).
        """

        async def fake_post(self, url, json=None, **kwargs):
            return _FakeHttpResponse(json_data={"error": "bad gateway"})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        r = VLLMReranker(base_url="http://gpu:8002")
        with pytest.raises(ValueError, match="results"):
            await r.rerank("q", ["a"], top_n=1)

    async def test_top_n_clamped_to_document_count(self, monkeypatch):
        """top_n > len(documents) is clamped in the request body (some servers
        reject top_n > candidates with a 4xx)."""
        calls: list[tuple[str, dict]] = []

        async def fake_post(self, url, json=None, **kwargs):
            calls.append((url, json))
            return _FakeHttpResponse(json_data={"results": [{"index": 0, "relevance_score": 0.5}]})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        r = VLLMReranker(base_url="http://gpu:8002")
        out = await r.rerank("q", ["only-one"], top_n=10)

        assert calls[0][1]["top_n"] == 1  # clamped to len(documents), not 10
        assert out == [{"index": 0, "relevance_score": 0.5}]

    async def test_malformed_result_item_raises_clear_value_error(self, monkeypatch):
        """A 'results' list whose items lack index/relevance_score fails with a
        clear ValueError naming the offending item, not an opaque KeyError."""

        async def fake_post(self, url, json=None, **kwargs):
            return _FakeHttpResponse(
                json_data={"results": [{"idx": 0, "score": 0.9}]}  # wrong keys
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        r = VLLMReranker(base_url="http://gpu:8002")
        with pytest.raises(ValueError, match="result item 0"):
            await r.rerank("q", ["a"], top_n=1)

    async def test_non_numeric_relevance_score_raises_clear_value_error(self, monkeypatch):
        """A non-numeric relevance_score fails with a clear ValueError, not an
        opaque float() ValueError."""

        async def fake_post(self, url, json=None, **kwargs):
            return _FakeHttpResponse(
                json_data={"results": [{"index": 0, "relevance_score": "high"}]}
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        r = VLLMReranker(base_url="http://gpu:8002")
        with pytest.raises(ValueError, match="not a number"):
            await r.rerank("q", ["a"], top_n=1)

    async def test_out_of_range_index_raises_clear_value_error(self, monkeypatch):
        """A server 'index' outside [0, len(documents)) fails with a clear
        ValueError instead of an opaque downstream IndexError (candidates[idx])."""

        async def fake_post(self, url, json=None, **kwargs):
            return _FakeHttpResponse(
                json_data={"results": [{"index": 5, "relevance_score": 0.9}]}  # only 2 docs
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        r = VLLMReranker(base_url="http://gpu:8002")
        with pytest.raises(ValueError, match="index"):
            await r.rerank("q", ["a", "b"], top_n=2)

    async def test_non_int_index_raises_clear_value_error(self, monkeypatch):
        """A non-int 'index' (string, or bool) fails with a clear ValueError
        instead of an opaque downstream TypeError (candidates[idx])."""

        async def fake_post(self, url, json=None, **kwargs):
            return _FakeHttpResponse(
                json_data={"results": [{"index": "0", "relevance_score": 0.9}]}  # str, not int
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

        r = VLLMReranker(base_url="http://gpu:8002")
        with pytest.raises(ValueError, match="index"):
            await r.rerank("q", ["a"], top_n=1)


class TestRerankBaseUrlSelection:
    """get_active_provider: rerank_base_url switches the local path to VLLMReranker."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return RerankerService(mock_db)

    def _ctx_config(self, provider, use_rerank, model):
        cfg = MagicMock()
        cfg.reranker_provider = provider
        cfg.use_rerank = use_rerank
        cfg.reranker_model = model
        return cfg

    async def test_rerank_base_url_set_returns_vllm_reranker(self, service, mock_db, monkeypatch):
        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.rerank_base_url = "http://gpu:8002"
        settings.rerank_model = "qwen3-reranker-0.6b"
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, "qwen3-reranker-0.6b")
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )

        assert isinstance(provider, VLLMReranker)
        assert provider.base_url == "http://gpu:8002"
        assert provider.model == "qwen3-reranker-0.6b"

    async def test_rerank_base_url_empty_keeps_self_hosted_reranker(
        self, service, mock_db, monkeypatch
    ):
        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.rerank_base_url = ""
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, "custom-ollama-model")
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )

        assert isinstance(provider, SelfHostedReranker)
        assert provider.model == "custom-ollama-model"

    async def test_vllm_path_replaces_stale_non_local_default_model(
        self, service, mock_db, monkeypatch
    ):
        """A stale voyage/cohere default model falls back to the built-in
        default when RERANK_MODEL is explicitly blanked (the `or DEFAULT` floor)."""
        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.rerank_base_url = "http://gpu:8002"
        settings.rerank_model = ""  # blanked → fall through to the built-in floor
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, "rerank-2")
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )

        assert isinstance(provider, VLLMReranker)
        assert provider.model == DEFAULT_VLLM_RERANK_MODEL

    async def test_vllm_path_replaces_stale_voyage_2_5_default_model(
        self, service, mock_db, monkeypatch
    ):
        """A stale Voyage 'rerank-2.5-lite' default is filtered → vllm default.

        Regression for the non_ollama_defaults gap: Voyage's documented default
        must not leak to the local reranker.
        """
        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.rerank_base_url = "http://gpu:8002"
        settings.rerank_model = ""  # blanked → exercise the built-in floor
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, "rerank-2.5-lite")
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )

        assert isinstance(provider, VLLMReranker)
        assert provider.model == DEFAULT_VLLM_RERANK_MODEL

    async def test_rerank_model_env_is_the_ops_default_for_unset_context(
        self, service, mock_db, monkeypatch
    ):
        """When the context has no reranker_model, the ops-level RERANK_MODEL
        (settings.rerank_model) is used — not the hardcoded built-in."""
        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.rerank_base_url = "http://gpu:8002"
        settings.rerank_model = "qwen3-reranker-4b"
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, None)  # context model unset
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )

        assert isinstance(provider, VLLMReranker)
        assert provider.model == "qwen3-reranker-4b"

    async def test_stale_context_model_falls_back_to_rerank_model_env(
        self, service, mock_db, monkeypatch
    ):
        """A stale remote-provider default is replaced by the ops-level
        RERANK_MODEL (not the built-in) when RERANK_MODEL is set."""
        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.rerank_base_url = "http://gpu:8002"
        settings.rerank_model = "bge-reranker-v2-m3"
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, "rerank-multilingual-v3.0")  # stale
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )

        assert isinstance(provider, VLLMReranker)
        assert provider.model == "bge-reranker-v2-m3"

    async def test_explicit_context_model_wins_over_rerank_model_env(
        self, service, mock_db, monkeypatch
    ):
        """An explicit, non-stale per-context reranker_model overrides the
        ops-level RERANK_MODEL (finer-grained config wins)."""
        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.rerank_base_url = "http://gpu:8002"
        settings.rerank_model = "qwen3-reranker-4b"
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, "my-finetuned-xenc")
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )

        assert isinstance(provider, VLLMReranker)
        assert provider.model == "my-finetuned-xenc"
