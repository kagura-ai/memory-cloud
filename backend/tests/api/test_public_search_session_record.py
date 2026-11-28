"""A signed-in session is served like an anonymous caller on public search (#2823).

The session middleware stores the record the sign-in wrote (``sub``,
``user_id``, ``email``, ``name``, ``picture``, ``role`` and the two times);
the record names no workspace. ``POST /api/v1/public/{context_id}/search``
does not read the session: a request with the cookie is served, counted and
refused exactly like one without it (the per-minute bucket, the plan pause,
the capacity check as an outsider, the public usage budget; no reranking).

These tests run the router behind the real ``SessionMiddleware`` over a stub
session manager whose synchronous ``get_session`` returns that record, so the
route sees exactly what a browser with a session cookie produces.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.middleware.session import SessionMiddleware
from api.routes import public_search as route
from auth.session import SESSION_COOKIE_NAME
from db.base import get_db
from models.auth import Context, Workspace
from services.public_usage_budget import BudgetCharge
from utils.exceptions import MemoryCloudException

LIMIT = 25000
OWNER_ID = "owner-user"


class _StubSessionManager:
    """Answers ``get_session`` synchronously, as ``SessionManager`` does."""

    def __init__(self, record: dict[str, Any] | None) -> None:
        self.record = record
        self.calls: list[str] = []

    def get_session(self, session_id: str, update_access: bool = True) -> dict[str, Any] | None:
        self.calls.append(session_id)
        return self.record


async def _refusal(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, MemoryCloudException)
    return JSONResponse(status_code=exc.status_code, content={"error": exc.error_code})


def _session_record(user_id: str) -> dict[str, Any]:
    """The record the session middleware puts on ``request.state.user``."""
    return {
        "sub": user_id,
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "name": "Signed In",
        "picture": None,
        "role": "user",
        "created_at": "2026-11-15T00:00:00+00:00",
        "last_accessed": "2026-11-15T00:00:00+00:00",
    }


class _Rig:
    def __init__(self, record: dict[str, Any] | None) -> None:
        workspace_id = uuid4()
        self.context = SimpleNamespace(
            id=uuid4(),
            workspace_id=workspace_id,
            is_public=True,
            resource_id=None,
            name="pub",
            deleted_at=None,
        )
        self.workspace = Workspace(
            id=workspace_id, name="w", plan_name="pro", owner_user_id=OWNER_ID
        )
        self.sessions = _StubSessionManager(record)
        self.bucket_keys: list[str] = []
        self.charge = AsyncMock(
            return_value=BudgetCharge(
                admitted=True,
                calls_used=1,
                period_started_at=datetime(2026, 11, 15, 8),
                period_seq=1,
                retry_after_seconds=None,
            )
        )
        self.capacity = AsyncMock()
        self.search = MagicMock()
        self.search.return_value.hybrid_search = AsyncMock(return_value=[])
        self.log_usage = AsyncMock()
        # What the route's one read of the returned rows answers (#3035, K4a).
        self.filter_rows: list[Any] = []

    async def _incr(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        self.bucket_keys.append(key)
        return len(self.bucket_keys)

    def post(self, *, cookie: bool = True):
        async def _get(model, _id):
            return {Context: self.context, Workspace: self.workspace}[model]

        db = MagicMock()
        db.get = AsyncMock(side_effect=_get)
        db.commit = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=self.filter_rows)))

        async def _db():
            yield db

        app = FastAPI()
        app.include_router(route.router, prefix="/api/v1")
        app.add_middleware(SessionMiddleware, session_manager=self.sessions)
        app.add_exception_handler(MemoryCloudException, _refusal)
        app.dependency_overrides[get_db] = _db

        with (
            patch.object(route, "incrby_counter", self._incr),
            patch.object(route, "_has_live_publication", AsyncMock(return_value=True)),
            patch.object(route, "public_usage_budget_limit", lambda: LIMIT),
            patch.object(route, "charge_public_usage", self.charge),
            patch.object(route, "ensure_not_capacity_locked", self.capacity),
            patch.object(route, "SearchService", self.search),
            patch.object(route, "log_usage", self.log_usage),
            TestClient(app) as client,
        ):
            if cookie:
                client.cookies.set(SESSION_COOKIE_NAME, "session-cookie-value")
            return client.post(
                f"/api/v1/public/{self.context.id}/search",
                json={"query": "q", "limit": 3, "use_rerank": True},
            )

    def assert_served_as_anonymous(self, response) -> None:
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"
        # Counted in the context's shared per-minute bucket.
        assert self.bucket_keys == [f"public_search:{self.context.id}:minute"]
        # One unit of the context workspace's public usage budget.
        self.charge.assert_awaited_once()
        assert self.charge.await_args.args == (self.context.workspace_id,)
        # Capacity is checked as an outsider (redacted refusal).
        self.capacity.assert_awaited_once()
        assert self.capacity.await_args.kwargs == {"outsider": True}
        # No reranking without a bound key, and logged as anonymous.
        kwargs = self.search.return_value.hybrid_search.await_args.kwargs
        assert kwargs["use_rerank"] is False
        assert self.log_usage.await_args.kwargs["user_id"] == "anonymous"


def test_a_member_session_is_served_like_an_anonymous_caller() -> None:
    rig = _Rig(_session_record(OWNER_ID))
    response = rig.post()
    # The cookie reached the session middleware; the route ignored the record.
    assert rig.sessions.calls == ["session-cookie-value"]
    assert response.status_code != 400
    rig.assert_served_as_anonymous(response)


def test_a_session_of_another_workspace_is_served_like_an_anonymous_caller() -> None:
    rig = _Rig(_session_record("someone-else"))
    response = rig.post()
    assert rig.sessions.calls == ["session-cookie-value"]
    assert response.status_code != 400
    rig.assert_served_as_anonymous(response)


def test_the_same_request_without_the_cookie_gets_the_same_answer() -> None:
    with_cookie, without_cookie = _Rig(_session_record(OWNER_ID)), _Rig(None)
    first = with_cookie.post()
    second = without_cookie.post(cookie=False)
    without_cookie.assert_served_as_anonymous(second)
    assert first.status_code == second.status_code == 200
    assert first.json().keys() == second.json().keys()


@pytest.mark.parametrize("user_id", [OWNER_ID, "someone-else"])
def test_a_session_does_not_skip_the_per_minute_bucket(user_id: str) -> None:
    rig = _Rig(_session_record(user_id))

    async def full(key: str, amount: int = 1, ttl: int = 60) -> int:
        rig.bucket_keys.append(key)
        return 51

    rig._incr = full  # type: ignore[method-assign]
    response = rig.post()
    assert response.status_code == 429
    assert response.json()["error"] == "RATE-001"
    rig.charge.assert_not_awaited()
    rig.search.return_value.hybrid_search.assert_not_awaited()


def test_a_resource_point_hit_answers_the_row_memory_id() -> None:
    """#2953: the point id of a resource document is not the row's id.

    #3035 (K4a): the row is answered once, at its first position, when the
    read of the returned rows finds it live in the context.
    """
    rig = _Rig(None)
    point_id, row_id = str(uuid4()), str(uuid4())
    rig.search.return_value.hybrid_search = AsyncMock(
        return_value=[
            {"id": point_id, "score": 0.9, "payload": {"memory_id": row_id, "summary": "s"}},
            {"id": row_id, "score": 0.5, "payload": {"summary": "written memory"}},
        ]
    )
    rig.filter_rows.append(SimpleNamespace(id=UUID(row_id), resource_doc_id="doc-1"))
    response = rig.post(cookie=False)
    assert response.status_code == 200, response.text
    ids = [r["memory_id"] for r in response.json()["results"]]
    assert ids == [row_id]
