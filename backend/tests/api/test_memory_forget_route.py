"""``POST /api/v1/memory/forget``: what the route itself decides.

The service is mocked. Forgetting by query searches one context, and this
route takes no ``context_id``, so a query without a ``memory_id`` is a 422
before the service is called (it used to reach the service and fail there).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from models.schemas import ForgetResponse


def _client(service: MagicMock, user: dict[str, Any] | None = None) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes.memory import get_memory_service, router
    from auth.dependencies import get_user_from_api_key_or_session

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/memory")
    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: user or {"user_id": "u1"}
    app.dependency_overrides[get_memory_service] = lambda: service
    return TestClient(app)


def test_forget_by_query_is_a_validation_error() -> None:
    service = MagicMock(forget=AsyncMock())
    with _client(service) as client:
        response = client.post("/api/v1/memory/forget", json={"query": "failover", "k": 5})
    assert response.status_code == 422
    assert "context_id" in response.json()["detail"]
    service.forget.assert_not_awaited()


def test_forget_by_id_passes_the_key_scope() -> None:
    memory_id, key_ws = uuid4(), uuid4()
    service = MagicMock(
        forget=AsyncMock(return_value=ForgetResponse(deleted_count=1, memory_ids=[memory_id]))
    )
    user = {"user_id": "u1", "api_key_workspace_id": key_ws, "current_workspace_id": key_ws}
    with _client(service, user) as client:
        response = client.post(
            "/api/v1/memory/forget", json={"memory_id": str(memory_id), "query": "ignored"}
        )
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 1
    kwargs = service.forget.await_args.kwargs
    assert kwargs["key_workspace_id"] == key_ws
    assert kwargs["current_context_id"] is None
