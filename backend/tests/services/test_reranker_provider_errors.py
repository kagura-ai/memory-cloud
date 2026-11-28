"""Voyage and Cohere rerank failures: what is logged and raised, and the time limit.

A provider's error message can carry the response body (voyageai puts the
API's ``detail``, and for some statuses the raw body and headers, into it;
cohere's ``ApiError`` prints its body). The failure line and the raised error
carry the error class and the HTTP status only. A call that does not answer
within ``RERANK_PROVIDER_TIMEOUT_SECONDS`` fails like any other provider error,
so the recall falls back to the un-reranked results.

The provider clients are imported lazily inside ``rerank()``, so fakes are put
into ``sys.modules``; nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest
import structlog

import services.reranker_service as reranker_module
from services.reranker_service import CohereReranker, VoyageReranker
from utils.exceptions import CohereError, VoyageError

MARKER = "provider-said-this-7f3a"


class _Result:
    def __init__(self, index: int, relevance_score: float) -> None:
        self.index = index
        self.relevance_score = relevance_score


class _Response:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results


class _VoyageApiError(Exception):
    """Shaped like voyageai.error.VoyageError: the message and ``http_status``."""

    def __init__(self, message: str, http_status: int | None) -> None:
        super().__init__(message)
        self.http_status = http_status


class _CohereApiError(Exception):
    """Shaped like cohere.core.ApiError: ``status_code`` and a printed body."""

    def __init__(self, status_code: int | None, body: Any) -> None:
        self.status_code = status_code
        self.body = body

    def __str__(self) -> str:
        return f"status_code: {self.status_code}, body: {self.body}"


def _fake_voyage(monkeypatch, rerank, created: list[dict] | None = None) -> None:
    module = types.ModuleType("voyageai")

    class AsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            if created is not None:
                created.append(kwargs)

        async def rerank(self, **kwargs: Any) -> Any:
            return await rerank(**kwargs)

    module.AsyncClient = AsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "voyageai", module)


def _fake_cohere(monkeypatch, rerank, created: list[dict] | None = None) -> None:
    module = types.ModuleType("cohere")

    class AsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if created is not None:
                created.append({"args": args, **kwargs})

        async def rerank(self, **kwargs: Any) -> Any:
            return await rerank(**kwargs)

    module.AsyncClient = AsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cohere", module)


def _failure_line(logs: list[dict], event: str) -> dict:
    (line,) = [entry for entry in logs if entry["event"] == event]
    return line


class TestVoyageFailure:
    async def test_the_failure_line_has_class_and_status_but_no_provider_text(self, monkeypatch):
        async def _fail(**kwargs: Any) -> Any:
            raise _VoyageApiError(f"Invalid request: {MARKER}", http_status=400)

        _fake_voyage(monkeypatch, _fail)
        with structlog.testing.capture_logs() as logs, pytest.raises(VoyageError) as raised:
            await VoyageReranker("k", model="rerank-2.5").rerank("q", ["d1", "d2"], 2)

        line = _failure_line(logs, "voyage_rerank_failed")
        assert line["error_class"] == "_VoyageApiError"
        assert line["status_code"] == 400
        assert line["model"] == "rerank-2.5"
        assert line["doc_count"] == 2
        assert "error" not in line
        assert MARKER not in repr(logs)
        assert MARKER not in str(raised.value)
        assert "Reranking failed for model rerank-2.5" in str(raised.value)
        assert isinstance(raised.value.__cause__, _VoyageApiError)

    async def test_an_error_without_a_status_logs_none(self, monkeypatch):
        async def _fail(**kwargs: Any) -> Any:
            raise RuntimeError(MARKER)

        _fake_voyage(monkeypatch, _fail)
        with structlog.testing.capture_logs() as logs, pytest.raises(VoyageError):
            await VoyageReranker("k").rerank("q", ["d"], 1)

        line = _failure_line(logs, "voyage_rerank_failed")
        assert line["error_class"] == "RuntimeError"
        assert line["status_code"] is None
        assert MARKER not in repr(logs)

    async def test_the_client_gets_the_time_limit(self, monkeypatch):
        created: list[dict] = []

        async def _ok(**kwargs: Any) -> Any:
            return _Response([_Result(0, 0.5)])

        _fake_voyage(monkeypatch, _ok, created)
        await VoyageReranker("k").rerank("q", ["d"], 1)

        assert created == [
            {"api_key": "k", "timeout": reranker_module.RERANK_PROVIDER_TIMEOUT_SECONDS}
        ]

    async def test_a_stuck_call_fails_within_the_time_limit(self, monkeypatch):
        monkeypatch.setattr(reranker_module, "RERANK_PROVIDER_TIMEOUT_SECONDS", 0.05)

        async def _stuck(**kwargs: Any) -> Any:
            await asyncio.sleep(30)

        _fake_voyage(monkeypatch, _stuck)
        with structlog.testing.capture_logs() as logs, pytest.raises(VoyageError):
            await asyncio.wait_for(VoyageReranker("k").rerank("q", ["d"], 1), timeout=5)

        assert _failure_line(logs, "voyage_rerank_failed")["error_class"] == "TimeoutError"


class TestCohereFailure:
    async def test_the_failure_line_has_class_and_status_but_no_provider_text(self, monkeypatch):
        async def _fail(**kwargs: Any) -> Any:
            raise _CohereApiError(422, {"message": MARKER})

        _fake_cohere(monkeypatch, _fail)
        with structlog.testing.capture_logs() as logs, pytest.raises(CohereError) as raised:
            await CohereReranker("k").rerank("q", ["d"], 1)

        line = _failure_line(logs, "cohere_rerank_failed")
        assert line["error_class"] == "_CohereApiError"
        assert line["status_code"] == 422
        assert "error" not in line
        assert MARKER not in repr(logs)
        assert MARKER not in str(raised.value)
        assert "Reranking failed for model rerank-multilingual-v3.0" in str(raised.value)

    async def test_the_client_gets_the_time_limit(self, monkeypatch):
        created: list[dict] = []

        async def _ok(**kwargs: Any) -> Any:
            return _Response([_Result(0, 0.5)])

        _fake_cohere(monkeypatch, _ok, created)
        await CohereReranker("k").rerank("q", ["d"], 1)

        assert created == [
            {"args": ("k",), "timeout": reranker_module.RERANK_PROVIDER_TIMEOUT_SECONDS}
        ]

    async def test_a_stuck_call_fails_within_the_time_limit(self, monkeypatch):
        monkeypatch.setattr(reranker_module, "RERANK_PROVIDER_TIMEOUT_SECONDS", 0.05)

        async def _stuck(**kwargs: Any) -> Any:
            await asyncio.sleep(30)

        _fake_cohere(monkeypatch, _stuck)
        with structlog.testing.capture_logs() as logs, pytest.raises(CohereError):
            await asyncio.wait_for(CohereReranker("k").rerank("q", ["d"], 1), timeout=5)

        assert _failure_line(logs, "cohere_rerank_failed")["error_class"] == "TimeoutError"


def test_the_time_limit_is_below_the_mcp_recall_tool_default():
    # The MCP recall tool's default timeout is 60 seconds (MCP_TIMEOUT_RECALL):
    # a stuck rerank must fail open well before the recall itself times out.
    assert 0 < reranker_module.RERANK_PROVIDER_TIMEOUT_SECONDS < 60
