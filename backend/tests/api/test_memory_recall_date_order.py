"""``POST /memory/recall`` with ``order="date"`` (#3004).

The REST twin of the MCP option, so the SDKs can pass it through: the same
rows, oldest first, each with ``date`` / ``date_source``, and ``order: "date"``
on the response. Omitted or ``"relevance"``, the response is the one served
before. ``include_superseded`` defaults to true under ``order="date"`` unless
the caller sent it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.routes.memory import recall
from models.schemas import MemoryResponse, RecallRequest, RecallResponse


def _row(created: datetime, summary: str, **kw: Any) -> MemoryResponse:
    return MemoryResponse(
        memory_id=uuid4(),
        summary=summary,
        context_summary=None,
        type="fact",
        importance=0.5,
        scope="persistent",
        created_at=created,
        client="api",
        tags=[],
        context=None,
        score=0.5,
        **kw,
    )


def _rows() -> list[MemoryResponse]:
    return [
        _row(datetime(2026, 9, 3, tzinfo=UTC), "new"),
        _row(datetime(2026, 9, 1, tzinfo=UTC), "old", superseded_by=uuid4()),
        _row(datetime(2026, 9, 9, tzinfo=UTC), "mid", observed_at="2026-09-02T10:00:00Z"),
    ]


class TestRequestModel:
    def test_default_is_relevance_and_include_superseded_false(self):
        request = RecallRequest(query="q")
        assert request.order == "relevance"
        assert request.include_superseded is False

    def test_date_defaults_include_superseded_true(self):
        assert RecallRequest(query="q", order="date").include_superseded is True

    def test_explicit_false_wins(self):
        request = RecallRequest(query="q", order="date", include_superseded=False)
        assert request.include_superseded is False

    def test_relevance_leaves_include_superseded_alone(self):
        assert RecallRequest(query="q", order="relevance").include_superseded is False

    @pytest.mark.parametrize("value", ["newest", "DATE", "", 1, None])
    def test_unknown_value_is_refused(self, value):
        with pytest.raises(ValidationError):
            RecallRequest(query="q", order=value)


async def _call(request: RecallRequest, rows: list[MemoryResponse]) -> RecallResponse:
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=RecallResponse(results=rows))
    ctx = uuid4()
    request.filters = {"context_id": str(ctx)}
    context = SimpleNamespace(id=ctx, workspace_id=uuid4(), is_private=False)
    with patch(
        "api.routes.memory.PermissionService",
        return_value=MagicMock(resolve_context_for_workspace_read=AsyncMock(return_value=context)),
    ):
        return await recall(
            request=request,
            user={"user_id": "u1", "current_workspace_id": uuid4()},
            memory_service=svc,
            db=MagicMock(),
        )


class TestRoute:
    @pytest.mark.asyncio
    async def test_date_order(self):
        result = await _call(RecallRequest(query="q", order="date"), _rows())
        assert result.order == "date"
        assert [r.summary for r in result.results] == ["old", "mid", "new"]
        assert [(r.date, r.date_source) for r in result.results] == [
            ("2026-09-01", "created"),
            ("2026-09-02", "observed"),
            ("2026-09-03", "created"),
        ]
        assert result.results[0].superseded_by is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("order", [None, "relevance"])
    async def test_relevance_returns_the_service_response(self, order):
        rows = _rows()
        kw = {} if order is None else {"order": order}
        result = await _call(RecallRequest(query="q", **kw), rows)
        assert [r.summary for r in result.results] == ["new", "old", "mid"]
        assert result.order is None


def _app(rows: list[MemoryResponse]) -> TestClient:
    """The route's response model serializes exactly as served."""
    app = FastAPI()

    @app.post("/r", response_model=RecallResponse, response_model_exclude_none=True)
    async def route(request: RecallRequest) -> RecallResponse:
        return await _call(request, rows)

    return TestClient(app)


class TestWire:
    def test_default_body_has_no_new_keys(self):
        rows = _rows()
        client = _app(rows)
        omitted = client.post("/r", json={"query": "q"}).text
        relevance = client.post("/r", json={"query": "q", "order": "relevance"}).text
        assert omitted == relevance
        assert '"order"' not in omitted
        assert '"date' not in omitted

    def test_date_body(self):
        body = _app(_rows()).post("/r", json={"query": "q", "order": "date"}).json()
        assert body["order"] == "date"
        assert [r["date"] for r in body["results"]] == ["2026-09-01", "2026-09-02", "2026-09-03"]
        assert [r["date_source"] for r in body["results"]] == ["created", "observed", "created"]

    def test_unknown_value_is_422(self):
        response = _app(_rows()).post("/r", json={"query": "q", "order": "newest"})
        assert response.status_code == 422


# ============================================================================
# The share-key /recall: the same request model, the same option
# ============================================================================


SHARE_CTX = uuid4()


def _share_client(svc: AsyncMock) -> TestClient:
    """``share_recall`` behind its response model, with a fixed share-key principal."""
    from api.routes.share_keys import share_recall

    principal = {
        "user_id": "owner-1",
        "role": "share-key",
        "current_context_id": SHARE_CTX,
        "current_workspace_id": uuid4(),
        "share_key_id": 7,
        "share_key_context_id": SHARE_CTX,
        "scope": "memory:read",
    }
    app = FastAPI()

    @app.post("/share/recall", response_model=RecallResponse, response_model_exclude_none=True)
    async def route(request: RecallRequest) -> RecallResponse:
        with patch("api.routes.share_keys.ensure_context_not_capacity_locked", new=AsyncMock()):
            return await share_recall(request=request, principal=principal, memory_service=svc)

    return TestClient(app)


class TestShareKeyRecall:
    def test_date_order(self):
        svc = AsyncMock()
        svc.recall = AsyncMock(return_value=RecallResponse(results=_rows()))
        response = _share_client(svc).post(
            "/share/recall", json={"query": "q", "order": "date", "full": 3}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["order"] == "date"
        assert [r["summary"] for r in body["results"]] == ["old", "mid", "new"]
        assert [r["date"] for r in body["results"]] == ["2026-09-01", "2026-09-02", "2026-09-03"]
        assert [r["date_source"] for r in body["results"]] == ["created", "observed", "created"]
        # Still the bound context only, and still summaries only (full forced to 0).
        svc.recall.assert_awaited_once()
        request, kwargs = svc.recall.await_args.args[0], svc.recall.await_args.kwargs
        assert kwargs["current_context_id"] == SHARE_CTX
        assert request.full == 0
        assert request.order == "date"
        assert request.include_superseded is True

    def test_unknown_value_is_422(self):
        svc = AsyncMock()
        response = _share_client(svc).post("/share/recall", json={"query": "q", "order": "newest"})
        assert response.status_code == 422
        svc.recall.assert_not_awaited()
