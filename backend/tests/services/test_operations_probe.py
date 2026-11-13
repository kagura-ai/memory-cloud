"""The opt-in operations probe (Issue #2768), without a database.

- targets are deduplicated by (kind, provider, model);
- a call that raises or times out is ``failed`` with the class name only;
- the embedding probe goes to the provider, not to the embedding cache;
- the key-resolving services are never consulted (they raise here);
- a successful completion / embedding probe is ledgered without identity.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import Settings
from services.llm_providers import ProviderResponse, Usage
from services.operations import probe
from services.operations.llm_lanes import LaneSnapshot
from services.operations.probe import (
    PROBE_PROMPT,
    PROBE_TEXT,
    ProbeClients,
    ProbeCredentialMissing,
    ProbeResult,
    ProbeTarget,
    probe_targets,
    run_budget_seconds,
    run_probe,
)


def _lane(lane: str, kind: str, provider: str = "openai", model: str = "m1") -> LaneSnapshot:
    return LaneSnapshot(
        lane=lane,  # type: ignore[arg-type]
        provider=provider,
        model=model,
        credential_present=True,
        probe_kind=kind,  # type: ignore[arg-type]
    )


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def _key_resolution_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every path that could resolve a workspace's own key raises in these tests."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the probe must never resolve a workspace key")

    monkeypatch.setattr("services.llm_service.LLMService._resolve_api_key", _boom)
    monkeypatch.setattr("services.llm_service.LLMService._get_provider", _boom)
    monkeypatch.setattr("services.embedding_service.EmbeddingService._get_user_api_key", _boom)
    monkeypatch.setattr("services.embedding_service.EmbeddingService.embed_with_usage", _boom)
    monkeypatch.setattr("services.reranker_service.RerankerService.get_active_provider", _boom)


def test_targets_are_deduplicated_by_kind_provider_model() -> None:
    lanes = [
        _lane("embedding", "embedding", "openai", "text-embedding-3-small"),
        _lane("sleep_judge", "completion", "openai", "m1"),
        _lane("recall_selector", "completion", "openai", "m1"),
        _lane("supersede_verify", "completion", "openai", "m1"),
        _lane("analysis", "completion", "anthropic", "m2"),
        _lane("rerank", "rerank", "self_hosted", "rr"),
    ]
    targets = probe_targets(lanes)
    assert [(t.kind, t.provider, t.model) for t in targets] == [
        ("embedding", "openai", "text-embedding-3-small"),
        ("completion", "openai", "m1"),
        ("completion", "anthropic", "m2"),
        ("rerank", "self_hosted", "rr"),
    ]
    assert targets[1].lanes == ("sleep_judge", "recall_selector", "supersede_verify")


@pytest.mark.parametrize(("interval", "budget"), [(300, 120.0), (200, 100.0), (900, 120.0)])
def test_run_budget(interval: int, budget: float) -> None:
    assert run_budget_seconds(interval) == budget


class _StubProvider:
    def __init__(self, *, fail: BaseException | None = None, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, prompt: str, system_prompt: str | None = None, **kwargs: Any):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return ProviderResponse(
            content='{"secret": "model output"}',
            usage=Usage(total=12, input=10, output=2, cached=0),
        )


def _clients_with(monkeypatch: pytest.MonkeyPatch, provider: Any) -> ProbeClients:
    monkeypatch.setattr(probe, "platform_llm_provider", lambda name, settings: provider)
    return ProbeClients(_settings())


class TestCompletion:
    async def test_ok_discards_the_answer(self, monkeypatch) -> None:
        stub = _StubProvider()
        target = ProbeTarget("completion", "openai", "m1", ("sleep_judge",))
        result = await run_probe(target, _clients_with(monkeypatch, stub), _settings())
        assert result.ok and result.error_class is None
        assert stub.calls == [
            {"prompt": PROBE_PROMPT, "model": "m1", "temperature": 0.0, "max_tokens": 16}
        ]
        assert result.completion_usage == Usage(total=12, input=10, output=2, cached=0)
        assert "model output" not in repr(result)

    async def test_failure_keeps_the_class_name_only(self, monkeypatch) -> None:
        stub = _StubProvider(fail=PermissionError("key sk-live-123 rejected for ws 42"))
        target = ProbeTarget("completion", "openai", "m1", ("sleep_judge",))
        result = await run_probe(target, _clients_with(monkeypatch, stub), _settings())
        assert (result.ok, result.error_class) == (False, "PermissionError")
        assert "sk-live" not in repr(result)

    async def test_timeout(self, monkeypatch) -> None:
        stub = _StubProvider(delay=1.0)
        target = ProbeTarget("completion", "openai", "m1", ("sleep_judge",))
        result = await run_probe(
            target, _clients_with(monkeypatch, stub), _settings(), timeout=0.01
        )
        assert (result.ok, result.error_class) == (False, "TimeoutError")

    async def test_no_platform_credential(self, monkeypatch) -> None:
        target = ProbeTarget("completion", "openai", "m1", ("sleep_judge",))
        result = await run_probe(target, _clients_with(monkeypatch, None), _settings())
        assert (result.ok, result.error_class) == (False, ProbeCredentialMissing.__name__)

    async def test_one_provider_per_run(self, monkeypatch) -> None:
        built: list[str] = []

        def _build(name: str, settings: Settings) -> Any:
            built.append(name)
            return _StubProvider()

        monkeypatch.setattr(probe, "platform_llm_provider", _build)
        clients = ProbeClients(_settings())
        for model in ("m1", "m2"):
            await run_probe(
                ProbeTarget("completion", "self_hosted", model, ("analysis",)),
                clients,
                _settings(),
            )
        assert built == ["self_hosted"]


class TestEmbedding:
    async def test_calls_the_provider_not_the_cache(self, monkeypatch) -> None:
        create = AsyncMock(return_value=SimpleNamespace(usage=SimpleNamespace(total_tokens=4)))
        client = MagicMock()
        client.embeddings.create = create
        client.close = AsyncMock()
        emb = SimpleNamespace(client=client, wire_model="wire-m", extra={"dimensions": 512})
        monkeypatch.setattr(probe, "platform_embedding_client", lambda settings: emb)

        def _no_cache(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("the embedding probe must bypass the cache")

        monkeypatch.setattr("db.redis.get_cache", _no_cache)
        monkeypatch.setattr("services.embedding_service.get_cache", _no_cache)
        target = ProbeTarget("embedding", "openai", "text-embedding-3-small", ("embedding",))
        result = await run_probe(target, ProbeClients(_settings()), _settings())
        assert result.ok
        assert result.embedding_tokens == 4
        create.assert_awaited_once_with(model="wire-m", input=PROBE_TEXT, dimensions=512)
        client.close.assert_awaited_once()


class TestRerank:
    async def test_one_document(self, monkeypatch) -> None:
        reranker = MagicMock()
        reranker.rerank = AsyncMock(return_value=[{"index": 0, "relevance_score": 0.9}])
        monkeypatch.setattr(probe, "platform_reranker", lambda settings: reranker)
        target = ProbeTarget("rerank", "self_hosted", "rr", ("rerank",))
        result = await run_probe(target, ProbeClients(_settings()), _settings())
        assert result.ok
        reranker.rerank.assert_awaited_once_with(PROBE_TEXT, [PROBE_TEXT], top_n=1)


class TestLedger:
    async def _ledger(self, monkeypatch, result: ProbeResult) -> list[dict[str, Any]]:
        recorded: list[dict[str, Any]] = []

        class _Writer:
            def __init__(self, session: Any) -> None:
                pass

            async def record(self, **kwargs: Any) -> Any:
                recorded.append(kwargs)
                return object()

        session = MagicMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.close = AsyncMock()
        monkeypatch.setattr("services.llm_call_log_writer.LLMCallLogWriter", _Writer)
        monkeypatch.setattr("services.operations.job_status._session_factory", lambda: session)
        await probe.ledger_probe(result)
        return recorded

    async def test_completion_row_has_no_identity(self, monkeypatch) -> None:
        from datetime import UTC, datetime

        result = ProbeResult(
            target=ProbeTarget("completion", "openai", "m1", ("sleep_judge",)),
            ok=True,
            latency_ms=10,
            error_class=None,
            at=datetime(2026, 11, 13, tzinfo=UTC),
            completion_usage=Usage(total=12, input=10, output=2, cached=0),
        )
        (row,) = await self._ledger(monkeypatch, result)
        assert row["caller"] == "admin"
        assert row["paid_by"] == "platform"
        assert row["call_type"] == "completion"
        assert row["call_metadata"] == {"stage": "operations_probe", "target": "completion"}
        assert row["fail_on_error"] is False
        assert (row["input_tokens"], row["output_tokens"]) == (10, 2)
        assert not {"user_id", "workspace_id", "context_id"} & set(row)
        assert row["occurred_at"].tzinfo is None

    async def test_failed_and_rerank_probes_are_not_ledgered(self, monkeypatch) -> None:
        from datetime import UTC, datetime

        at = datetime(2026, 11, 13, tzinfo=UTC)
        failed = ProbeResult(ProbeTarget("completion", "openai", "m1", ()), False, 5, "X", at)
        rerank = ProbeResult(ProbeTarget("rerank", "self_hosted", "rr", ()), True, 5, None, at)
        assert await self._ledger(monkeypatch, failed) == []
        assert await self._ledger(monkeypatch, rerank) == []
