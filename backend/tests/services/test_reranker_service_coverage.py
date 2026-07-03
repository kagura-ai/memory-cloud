"""Coverage tests for services.reranker_service.

Targets:
- _parse_relevance_score: pure score-string parser (percent / fraction / plain / garbage)
- VoyageReranker / CohereReranker: input validation + success/error mapping
  (external clients are imported lazily inside rerank(), so we inject fakes
  into sys.modules — never a real network call)
- SelfHostedReranker: httpx.AsyncClient.post is patched to return canned JSON,
  raise_for_status errors, and timeouts — assert per-doc fallback + sort
- RerankerService: provider selection from a (mocked) DB session, Ollama
  context-config priority, decrypt path, model resolution, and the rerank()
  candidate-mapping / passthrough branches.

DB access for RerankerService is exercised through a MagicMock AsyncSession
(house style from test_quota_service.py) so we control exactly which ORM rows
each ``execute`` returns without touching FK-constrained tables.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from services.reranker_service import (
    DEFAULT_SELF_HOSTED_RERANK_MODEL,
    RERANKER_PROVIDERS,
    CohereReranker,
    RerankerService,
    SelfHostedReranker,
    VoyageReranker,
    _parse_relevance_score,
)
from utils.exceptions import CohereError, VoyageError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRerankResult:
    """Mimics a Voyage/Cohere rerank result item (index + relevance_score)."""

    def __init__(self, index: int, relevance_score: float):
        self.index = index
        self.relevance_score = relevance_score


class _FakeRerankResponse:
    def __init__(self, results):
        self.results = results


class _FakeHttpResponse:
    """Minimal stand-in for httpx.Response used by SelfHostedReranker."""

    def __init__(self, *, json_data=None, raise_exc=None):
        self._json = json_data or {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._json


# ---------------------------------------------------------------------------
# _parse_relevance_score (pure)
# ---------------------------------------------------------------------------


class TestParseRelevanceScore:
    """Parsing of model-emitted relevance strings into a clamped float."""

    def test_plain_decimal(self):
        """'0.83' parses to 0.83."""
        assert _parse_relevance_score("0.83") == pytest.approx(0.83)

    def test_plain_integer_zero_and_one(self):
        """Bare '0' and '1' parse to their float values."""
        assert _parse_relevance_score("0") == 0.0
        assert _parse_relevance_score("1") == 1.0

    def test_percentage(self):
        """'80%' converts to 0.8."""
        assert _parse_relevance_score("80%") == pytest.approx(0.8)

    def test_percentage_over_100_clamped(self):
        """'150%' clamps to 1.0."""
        assert _parse_relevance_score("150%") == 1.0

    def test_fraction(self):
        """'0.8/1' parses the numerator/denominator to 0.8."""
        assert _parse_relevance_score("0.8/1") == pytest.approx(0.8)

    def test_fraction_score_label(self):
        """'Score: 5/10' parses the embedded fraction to 0.5."""
        assert _parse_relevance_score("Score: 5/10") == pytest.approx(0.5)

    def test_fraction_zero_denominator_returns_zero(self):
        """Division by zero in a fraction yields 0.0, not a crash."""
        assert _parse_relevance_score("5/0") == 0.0

    def test_fraction_over_one_clamped(self):
        """A fraction > 1 (e.g. 9/4) clamps to 1.0."""
        assert _parse_relevance_score("9/4") == 1.0

    def test_plain_number_out_of_range_clamped(self):
        """A plain number above 1 clamps to 1.0."""
        assert _parse_relevance_score("7") == 1.0

    def test_embedded_plain_number(self):
        """A decimal embedded in prose is extracted."""
        assert _parse_relevance_score("relevance is 0.42 overall") == pytest.approx(0.42)

    def test_empty_string_returns_zero(self):
        """Empty input returns 0.0."""
        assert _parse_relevance_score("") == 0.0

    def test_whitespace_only_returns_zero(self):
        """Whitespace-only input returns 0.0 (stripped to empty)."""
        assert _parse_relevance_score("   ") == 0.0

    def test_garbage_returns_zero(self):
        """Non-numeric garbage returns 0.0."""
        assert _parse_relevance_score("not a number at all") == 0.0

    def test_percentage_takes_priority_over_plain(self):
        """When a '%' is present, percentage parsing wins over plain-number."""
        # "80%" would parse as 80.0 -> clamp 1.0 if treated as plain; percentage gives 0.8.
        assert _parse_relevance_score("80%") == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# VoyageReranker
# ---------------------------------------------------------------------------


class TestVoyageReranker:
    """VoyageReranker validation and success/error mapping."""

    def test_init_defaults(self):
        """Default model is rerank-2.5-lite and provider_name is voyage."""
        r = VoyageReranker("key-abc")
        assert r.api_key == "key-abc"
        assert r.model == "rerank-2.5-lite"
        assert r.provider_name == "voyage"

    async def test_empty_documents_returns_empty(self):
        """No documents short-circuits to an empty list (no API import)."""
        r = VoyageReranker("k")
        assert await r.rerank("q", [], 5) == []

    async def test_non_positive_top_n_raises_value_error(self):
        """top_n <= 0 raises ValueError before any API call."""
        r = VoyageReranker("k")
        with pytest.raises(ValueError, match="top_n must be positive"):
            await r.rerank("q", ["doc"], 0)

    async def test_success_maps_results(self, monkeypatch):
        """A successful voyage call maps results to index/relevance_score dicts."""
        fake_module = types.ModuleType("voyageai")

        class _Client:
            def __init__(self, api_key):
                self.api_key = api_key

            def rerank(self, query, documents, model, top_k):
                return _FakeRerankResponse([_FakeRerankResult(1, 0.9), _FakeRerankResult(0, 0.4)])

        fake_module.Client = _Client
        monkeypatch.setitem(sys.modules, "voyageai", fake_module)

        r = VoyageReranker("k", model="rerank-2.5")
        out = await r.rerank("query", ["a", "b"], 2)

        assert out == [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.4},
        ]

    async def test_api_failure_raises_voyage_error(self, monkeypatch):
        """An exception from the voyage client is wrapped in VoyageError."""
        fake_module = types.ModuleType("voyageai")

        class _Client:
            def __init__(self, api_key):
                pass

            def rerank(self, **kwargs):
                raise RuntimeError("boom")

        fake_module.Client = _Client
        monkeypatch.setitem(sys.modules, "voyageai", fake_module)

        r = VoyageReranker("k")
        with pytest.raises(VoyageError, match="Reranking failed"):
            await r.rerank("q", ["d"], 1)

    async def test_value_error_from_client_propagates(self, monkeypatch):
        """A ValueError raised inside the client is re-raised as-is, not wrapped."""
        fake_module = types.ModuleType("voyageai")

        class _Client:
            def __init__(self, api_key):
                pass

            def rerank(self, **kwargs):
                raise ValueError("bad arg")

        fake_module.Client = _Client
        monkeypatch.setitem(sys.modules, "voyageai", fake_module)

        r = VoyageReranker("k")
        with pytest.raises(ValueError, match="bad arg"):
            await r.rerank("q", ["d"], 1)


# ---------------------------------------------------------------------------
# CohereReranker
# ---------------------------------------------------------------------------


class TestCohereReranker:
    """CohereReranker validation and success/error mapping."""

    def test_init_defaults(self):
        """Default model is rerank-multilingual-v3.0 and provider_name is cohere."""
        r = CohereReranker("ck")
        assert r.model == "rerank-multilingual-v3.0"
        assert r.provider_name == "cohere"

    async def test_empty_documents_returns_empty(self):
        """No documents short-circuits to empty."""
        assert await CohereReranker("k").rerank("q", [], 3) == []

    async def test_non_positive_top_n_raises(self):
        """top_n <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="top_n must be positive"):
            await CohereReranker("k").rerank("q", ["d"], -1)

    async def test_success_maps_results(self, monkeypatch):
        """A successful cohere AsyncClient.rerank maps to index/relevance_score dicts."""
        fake_module = types.ModuleType("cohere")

        class _AsyncClient:
            def __init__(self, api_key):
                self.api_key = api_key

            async def rerank(self, model, query, documents, top_n):
                return _FakeRerankResponse([_FakeRerankResult(0, 0.7), _FakeRerankResult(2, 0.6)])

        fake_module.AsyncClient = _AsyncClient
        monkeypatch.setitem(sys.modules, "cohere", fake_module)

        out = await CohereReranker("k").rerank("q", ["a", "b", "c"], 2)
        assert out == [
            {"index": 0, "relevance_score": 0.7},
            {"index": 2, "relevance_score": 0.6},
        ]

    async def test_api_failure_raises_cohere_error(self, monkeypatch):
        """An exception from the cohere client is wrapped in CohereError."""
        fake_module = types.ModuleType("cohere")

        class _AsyncClient:
            def __init__(self, api_key):
                pass

            async def rerank(self, **kwargs):
                raise RuntimeError("cohere down")

        fake_module.AsyncClient = _AsyncClient
        monkeypatch.setitem(sys.modules, "cohere", fake_module)

        with pytest.raises(CohereError, match="Reranking failed"):
            await CohereReranker("k").rerank("q", ["d"], 1)

    async def test_value_error_from_client_propagates(self, monkeypatch):
        """A ValueError raised inside the cohere client is re-raised as-is."""
        fake_module = types.ModuleType("cohere")

        class _AsyncClient:
            def __init__(self, api_key):
                pass

            async def rerank(self, **kwargs):
                raise ValueError("bad cohere arg")

        fake_module.AsyncClient = _AsyncClient
        monkeypatch.setitem(sys.modules, "cohere", fake_module)

        with pytest.raises(ValueError, match="bad cohere arg"):
            await CohereReranker("k").rerank("q", ["d"], 1)


# ---------------------------------------------------------------------------
# SelfHostedReranker
# ---------------------------------------------------------------------------


def _patch_ollama_post(monkeypatch, handler):
    """Patch httpx.AsyncClient.post with an async ``handler(url, json=...)``."""

    async def _post(self, url, json=None, **kwargs):  # noqa: ANN001
        return handler(url, json)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)


class TestSelfHostedReranker:
    """SelfHostedReranker scoring, sorting, and per-doc error fallback."""

    def test_init_strips_trailing_slash(self):
        """base_url trailing slash is stripped; default model is used."""
        r = SelfHostedReranker("http://localhost:11434/")
        assert r.base_url == "http://localhost:11434"
        assert r.model == DEFAULT_SELF_HOSTED_RERANK_MODEL
        assert r.provider_name == "self_hosted"

    async def test_empty_documents_returns_empty(self):
        """No documents short-circuits to empty list."""
        assert await SelfHostedReranker("http://x").rerank("q", [], 3) == []

    async def test_non_positive_top_n_raises(self):
        """top_n <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="top_n must be positive"):
            await SelfHostedReranker("http://x").rerank("q", ["d"], 0)

    async def test_scores_sorted_descending_and_truncated(self, monkeypatch):
        """Docs are scored, sorted by score desc, and truncated to top_n."""

        # Map each prompt to a score by inspecting the document substring.
        def handler(url, json):
            prompt = json["prompt"]
            if "alpha" in prompt:
                return _FakeHttpResponse(json_data={"choices": [{"text": "0.2"}]})
            if "bravo" in prompt:
                return _FakeHttpResponse(json_data={"choices": [{"text": "0.9"}]})
            return _FakeHttpResponse(json_data={"choices": [{"text": "0.5"}]})

        _patch_ollama_post(monkeypatch, handler)

        out = await SelfHostedReranker("http://x").rerank(
            "q", ["alpha doc", "bravo doc", "charlie doc"], 2
        )

        assert [d["index"] for d in out] == [1, 2]  # bravo(0.9) then charlie(0.5)
        assert out[0]["relevance_score"] == pytest.approx(0.9)
        assert out[1]["relevance_score"] == pytest.approx(0.5)
        assert len(out) == 2

    async def test_http_error_falls_back_to_zero(self, monkeypatch):
        """A raise_for_status error on one doc yields score 0.0 for that doc."""

        def handler(url, json):
            if "good" in json["prompt"]:
                return _FakeHttpResponse(json_data={"choices": [{"text": "0.8"}]})
            return _FakeHttpResponse(
                raise_exc=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
            )

        _patch_ollama_post(monkeypatch, handler)

        out = await SelfHostedReranker("http://x").rerank("q", ["good", "bad"], 5)
        scores = {d["index"]: d["relevance_score"] for d in out}
        assert scores[0] == pytest.approx(0.8)
        assert scores[1] == 0.0

    async def test_timeout_falls_back_to_zero(self, monkeypatch):
        """A timeout exception during post yields 0.0 for that doc (no raise)."""

        def handler(url, json):
            raise httpx.TimeoutException("slow")

        _patch_ollama_post(monkeypatch, handler)

        out = await SelfHostedReranker("http://x").rerank("q", ["only"], 1)
        assert out == [{"index": 0, "relevance_score": 0.0}]

    async def test_malformed_completion_body_scores_zero(self, monkeypatch):
        """A malformed /v1/completions body (no 'choices') scores 0.0.

        The reranker reads ``resp.json()["choices"][0]["text"]``; an empty
        ``{}`` raises KeyError, which the per-doc ``except`` swallows to 0.0.
        This exercises that fallback path (also hit by ``{"choices": []}`` /
        ``{"choices": [{}]}`` from a real backend).
        """

        def handler(url, json):
            return _FakeHttpResponse(json_data={})

        _patch_ollama_post(monkeypatch, handler)

        out = await SelfHostedReranker("http://x").rerank("q", ["doc"], 1)
        assert out[0]["relevance_score"] == 0.0


# ---------------------------------------------------------------------------
# RerankerService — provider selection
# ---------------------------------------------------------------------------


def _result_scalar_one_or_none(value):
    """Build a mock execute() result whose scalar_one_or_none() returns value."""
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)
    return res


def _make_api_key_row(provider, encrypted_value, context_id=None):
    """Construct an ExternalAPIKey-like row for provider selection tests."""
    row = MagicMock()
    row.provider = provider
    row.encrypted_value = encrypted_value
    row.context_id = context_id
    return row


class TestRerankerServiceProviderSelection:
    """RerankerService.get_active_provider provider resolution paths."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return RerankerService(mock_db)

    async def test_no_workspace_returns_none(self, service, mock_db):
        """Without workspace_id and without an Ollama context config, returns None."""
        provider = await service.get_active_provider("user-1", context_id=None, workspace_id=None)
        assert provider is None
        mock_db.execute.assert_not_called()

    async def test_no_matching_key_returns_none(self, service, mock_db):
        """A workspace with no enabled reranker key returns None."""
        mock_db.execute.return_value = _result_scalar_one_or_none(None)
        provider = await service.get_active_provider(
            "user-1", context_id=None, workspace_id=str(uuid4())
        )
        assert provider is None

    async def test_voyage_key_returns_voyage_provider(self, service, mock_db, monkeypatch):
        """An enabled voyage key is decrypted and a VoyageReranker is returned."""
        from services import reranker_service as rs

        encryptor = MagicMock()
        encryptor.decrypt = MagicMock(return_value="decrypted-voyage-key")
        monkeypatch.setattr(rs, "get_encryptor", lambda: encryptor)

        row = _make_api_key_row("voyage", "enc-blob")
        mock_db.execute.return_value = _result_scalar_one_or_none(row)

        provider = await service.get_active_provider(
            "user-1", context_id=None, workspace_id=str(uuid4())
        )

        assert isinstance(provider, VoyageReranker)
        assert provider.api_key == "decrypted-voyage-key"
        assert provider.model == "rerank-2"  # default when no context_id
        encryptor.decrypt.assert_called_once_with("enc-blob")

    async def test_cohere_key_returns_cohere_provider(self, service, mock_db, monkeypatch):
        """An enabled cohere key is decrypted and a CohereReranker is returned."""
        from services import reranker_service as rs

        encryptor = MagicMock()
        encryptor.decrypt = MagicMock(return_value="decrypted-cohere-key")
        monkeypatch.setattr(rs, "get_encryptor", lambda: encryptor)

        row = _make_api_key_row("cohere", "enc")
        mock_db.execute.return_value = _result_scalar_one_or_none(row)

        provider = await service.get_active_provider(
            "user-1", context_id=None, workspace_id=str(uuid4())
        )

        assert isinstance(provider, CohereReranker)
        assert provider.api_key == "decrypted-cohere-key"
        assert provider.model == "rerank-multilingual-v3.0"

    async def test_unknown_provider_returns_none(self, service, mock_db, monkeypatch):
        """A key with an unrecognized provider name returns None after decrypt."""
        from services import reranker_service as rs

        encryptor = MagicMock()
        encryptor.decrypt = MagicMock(return_value="x")
        monkeypatch.setattr(rs, "get_encryptor", lambda: encryptor)

        row = _make_api_key_row("mystery", "enc")
        mock_db.execute.return_value = _result_scalar_one_or_none(row)

        provider = await service.get_active_provider(
            "user-1", context_id=None, workspace_id=str(uuid4())
        )
        assert provider is None

    async def test_workspace_id_uuid_object_accepted(self, service, mock_db, monkeypatch):
        """workspace_id passed as a UUID object (not str) is handled."""
        from services import reranker_service as rs

        encryptor = MagicMock()
        encryptor.decrypt = MagicMock(return_value="k")
        monkeypatch.setattr(rs, "get_encryptor", lambda: encryptor)
        mock_db.execute.return_value = _result_scalar_one_or_none(
            _make_api_key_row("voyage", "enc")
        )

        provider = await service.get_active_provider(
            "user-1", context_id=None, workspace_id=uuid4()
        )
        assert isinstance(provider, VoyageReranker)


class TestRerankerServiceOllamaContextConfig:
    """Ollama-from-context-config priority path in get_active_provider."""

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

    async def test_ollama_context_config_returns_ollama(self, service, mock_db, monkeypatch):
        """An ollama context config with use_rerank=True returns an SelfHostedReranker."""

        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.self_hosted_api_key = ""
        settings.rerank_base_url = ""  # not set → SelfHostedReranker, not the vLLM path
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, "custom-ollama-model")
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )

        assert isinstance(provider, SelfHostedReranker)
        assert provider.base_url == "http://ollama:11434"
        assert provider.model == "custom-ollama-model"

    async def test_ollama_config_non_ollama_default_model_replaced(
        self, service, mock_db, monkeypatch
    ):
        """A stale non-ollama default model is swapped for the ollama default."""

        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.self_hosted_api_key = ""
        settings.self_hosted_rerank_model = DEFAULT_SELF_HOSTED_RERANK_MODEL
        settings.rerank_base_url = ""  # not set → SelfHostedReranker, not the vLLM path
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, "rerank-multilingual-v3.0")
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )
        assert isinstance(provider, SelfHostedReranker)
        assert provider.model == DEFAULT_SELF_HOSTED_RERANK_MODEL

    async def test_ollama_config_empty_model_uses_default(self, service, mock_db, monkeypatch):
        """An empty reranker_model on an ollama config falls back to the ollama default."""
        settings = MagicMock()
        settings.self_hosted_base_url = "http://ollama:11434"
        settings.self_hosted_api_key = ""
        settings.self_hosted_rerank_model = DEFAULT_SELF_HOSTED_RERANK_MODEL
        settings.rerank_base_url = ""  # not set → SelfHostedReranker, not the vLLM path
        monkeypatch.setattr("config.settings.get_settings", lambda: settings, raising=True)

        cfg = self._ctx_config("self_hosted", True, None)
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )
        assert isinstance(provider, SelfHostedReranker)
        assert provider.model == DEFAULT_SELF_HOSTED_RERANK_MODEL

    async def test_ollama_config_use_rerank_false_falls_through(
        self, service, mock_db, monkeypatch
    ):
        """An ollama config with use_rerank=False does NOT short-circuit; falls
        through to the API-key path which (no workspace) returns None."""
        cfg = self._ctx_config("self_hosted", False, "m")
        # First execute() = context config lookup; no second call needed because
        # workspace_id is None so the API-key branch returns early.
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        provider = await service.get_active_provider(
            "user-1", context_id=str(uuid4()), workspace_id=None
        )
        assert provider is None

    async def test_non_ollama_context_config_falls_through_to_api_key(
        self, service, mock_db, monkeypatch
    ):
        """A non-ollama context config falls through; with a workspace + voyage
        key the API-key branch resolves a VoyageReranker."""
        from services import reranker_service as rs

        encryptor = MagicMock()
        encryptor.decrypt = MagicMock(return_value="vk")
        monkeypatch.setattr(rs, "get_encryptor", lambda: encryptor)

        cfg = self._ctx_config("voyage", True, "rerank-2")
        ctx_id = str(uuid4())
        # 1st execute = context config; 2nd = ExternalAPIKey row;
        # 3rd = ContextSearchConfigRepository.create_or_get (model resolution).
        api_row = _make_api_key_row("voyage", "enc", context_id=None)

        model_cfg = MagicMock()
        model_cfg.reranker_provider = "voyage"
        model_cfg.reranker_model = "rerank-2.5"
        model_result = MagicMock()
        model_result.scalar_one_or_none = MagicMock(return_value=model_cfg)

        mock_db.execute.side_effect = [
            _result_scalar_one_or_none(cfg),
            _result_scalar_one_or_none(api_row),
            model_result,
        ]

        provider = await service.get_active_provider(
            "user-1", context_id=ctx_id, workspace_id=str(uuid4())
        )
        assert isinstance(provider, VoyageReranker)
        assert provider.model == "rerank-2.5"


# ---------------------------------------------------------------------------
# RerankerService._get_reranker_model
# ---------------------------------------------------------------------------


class TestGetRerankerModel:
    """Model-name resolution helper."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return RerankerService(mock_db)

    async def test_no_context_id_voyage_default(self, service):
        """Without a context_id, voyage resolves to its default 'rerank-2'."""
        assert await service._get_reranker_model(None, "voyage") == "rerank-2"

    async def test_no_context_id_cohere_default(self, service):
        """Without a context_id, cohere resolves to its multilingual default."""
        assert await service._get_reranker_model(None, "cohere") == "rerank-multilingual-v3.0"

    async def test_no_context_id_unknown_provider_falls_back(self, service):
        """An unknown provider with no context falls back to 'rerank-2'."""
        assert await service._get_reranker_model(None, "weird") == "rerank-2"

    async def test_context_config_model_used(self, service, mock_db):
        """A context config with a reranker_model returns that model."""
        cfg = MagicMock()
        cfg.reranker_provider = "voyage"
        cfg.reranker_model = "rerank-2.5-lite"
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        model = await service._get_reranker_model(str(uuid4()), "voyage")
        assert model == "rerank-2.5-lite"

    async def test_context_config_provider_mismatch_still_uses_config_model(self, service, mock_db):
        """A provider mismatch logs a warning but still uses the config model."""
        cfg = MagicMock()
        cfg.reranker_provider = "cohere"
        cfg.reranker_model = "rerank-multilingual-v3.0"
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        model = await service._get_reranker_model(str(uuid4()), "voyage")
        assert model == "rerank-multilingual-v3.0"

    async def test_context_config_null_model_uses_provider_default(self, service, mock_db):
        """A config with a null reranker_model falls back to the provider default."""
        cfg = MagicMock()
        cfg.reranker_provider = "cohere"
        cfg.reranker_model = None
        mock_db.execute.return_value = _result_scalar_one_or_none(cfg)

        model = await service._get_reranker_model(str(uuid4()), "cohere")
        assert model == "rerank-multilingual-v3.0"

    async def test_repo_exception_falls_back_to_default(self, service, mock_db):
        """A DB error during config load falls back to the provider default."""
        mock_db.execute = AsyncMock(side_effect=RuntimeError("db down"))
        model = await service._get_reranker_model(str(uuid4()), "voyage")
        assert model == "rerank-2"


# ---------------------------------------------------------------------------
# RerankerService.rerank — orchestration
# ---------------------------------------------------------------------------


class TestRerankerServiceRerank:
    """End-to-end rerank() candidate mapping and passthrough behaviour."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return RerankerService(mock_db)

    def _candidate(self, summary, ctx_summary=None):
        payload = {"summary": summary}
        if ctx_summary is not None:
            payload["context_summary"] = ctx_summary
        return {"payload": payload}

    async def test_empty_candidates_returns_passthrough(self, service):
        """Empty candidate list is returned unchanged with no provider lookup."""
        assert await service.rerank("q", [], "u", 5) == []

    async def test_no_provider_returns_candidates_unchanged(self, service, monkeypatch):
        """When no provider is configured, candidates pass through unchanged."""
        monkeypatch.setattr(service, "get_active_provider", AsyncMock(return_value=None))
        candidates = [self._candidate("a"), self._candidate("b")]
        out = await service.rerank("q", candidates, "u", 5)
        assert out is candidates

    async def test_reranks_and_maps_scores(self, service, monkeypatch):
        """A provider's results reorder candidates and stamp rerank/hybrid scores."""
        fake_provider = MagicMock()
        fake_provider.provider_name = "voyage"
        fake_provider.rerank = AsyncMock(
            return_value=[
                {"index": 1, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.30},
            ]
        )
        monkeypatch.setattr(service, "get_active_provider", AsyncMock(return_value=fake_provider))

        candidates = [
            self._candidate("first summary", "ctx-a"),
            self._candidate("second summary"),
        ]
        out = await service.rerank("query", candidates, "u", 2)

        # Reordered: index 1 first, then index 0.
        assert out[0]["payload"]["summary"] == "second summary"
        assert out[0]["rerank_score"] == pytest.approx(0.95)
        assert out[0]["hybrid_score"] == pytest.approx(0.95)
        assert out[1]["payload"]["summary"] == "first summary"
        assert out[1]["rerank_score"] == pytest.approx(0.30)

        # Documents passed to the provider include the context_summary suffix.
        call_args = fake_provider.rerank.await_args
        documents = call_args.args[1]
        assert documents[0] == "first summary ctx-a"
        assert documents[1] == "second summary "  # missing context_summary -> ''


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """Sanity checks on module-level constants."""

    def test_reranker_providers_set(self):
        """RERANKER_PROVIDERS contains the two API-key providers."""
        assert RERANKER_PROVIDERS == {"cohere", "voyage"}

    def test_default_ollama_model_constant(self):
        """The default ollama rerank model constant is non-empty."""
        assert DEFAULT_SELF_HOSTED_RERANK_MODEL
        assert ":" in DEFAULT_SELF_HOSTED_RERANK_MODEL
