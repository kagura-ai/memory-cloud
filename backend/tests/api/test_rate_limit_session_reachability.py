"""Can the plan-gated rate-limit refusal fire for cookie-session traffic? (#1648)

**Verified answer: no.** ``RateLimitMiddleware`` is registered AFTER
``SessionMiddlewareWrapper`` in ``api/main.py``, which in Starlette means it
runs OUTSIDE it — so it executes before the session middleware has read the
``kagura_session`` cookie and set ``request.state.user_id``. The limiter's step
3 ("skip for unauthenticated users") therefore always sees ``user_id`` unset
and returns early: no per-minute check, no daily-quota check, and no
``QUOTA-001`` refusal, for web-UI traffic or for anything else that
authenticates below the middleware layer (API keys and OAuth bearer tokens are
resolved in FastAPI dependencies, i.e. even further in).

The refusal itself is intact when a caller hands the middleware a request that
already carries ``user_id`` — the last test pins that — so the dead part is the
*path to* it, not the code. This module does not change the middleware order or
the refusal; both are behaviour changes that need their own decision. It exists
so the claim is a test result instead of a code reading, and so a future reorder
that makes the refusal reachable fails loudly here first.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import SessionMiddlewareWrapper
from api.middleware.rate_limit import RateLimitMiddleware

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
SESSION_MIDDLEWARE = SRC_ROOT / "api" / "middleware" / "session.py"

PROBE_PATH = "/api/v1/users/me"  # a plain REST path: not excluded, not MCP, not public
SESSION_USER = {"user_id": "session-user-1648", "email": "user@test.invalid", "role": "user"}


class _StubSessionManager:
    """Minimal SessionManager: any cookie value resolves to one known session."""

    def get_session(self, session_id: str) -> dict | None:
        return dict(SESSION_USER) if session_id else None


def _replica_stack() -> tuple[FastAPI, list[str | None]]:
    """An app with ``api/main.py``'s registration order and a recording limiter.

    A replica rather than the real app because the real one needs a database,
    Redis and a lifespan; ``test_registration_order_matches_the_real_app``
    below is what keeps the replica honest.
    """
    seen_user_ids: list[str | None] = []

    class RecordingRateLimitMiddleware(RateLimitMiddleware):
        async def dispatch(self, request: Request, call_next):
            seen_user_ids.append(getattr(request.state, "user_id", None))
            return await super().dispatch(request, call_next)

    app = FastAPI()

    @app.get(PROBE_PATH)
    async def _probe(request: Request) -> dict:
        return {"user_id": getattr(request.state, "user_id", None)}

    # Same two lines, same order, as api/main.py.
    app.add_middleware(SessionMiddlewareWrapper)
    app.add_middleware(RecordingRateLimitMiddleware)
    return app, seen_user_ids


def test_registration_order_matches_the_real_app() -> None:
    """The premise: the limiter is registered later, so Starlette runs it outer."""
    names = [middleware.cls.__name__ for middleware in api_main.app.user_middleware]
    assert "RateLimitMiddleware" in names and "SessionMiddlewareWrapper" in names
    # user_middleware is outermost-first; a lower index runs earlier.
    assert names.index("RateLimitMiddleware") < names.index("SessionMiddlewareWrapper"), (
        "RateLimitMiddleware no longer runs before SessionMiddlewareWrapper. The "
        "plan-gated QUOTA-001 refusal may now be reachable for cookie-session "
        "traffic — re-read its wording (it hardcodes legacy tier names) and the "
        "client handling before shipping the reorder."
    )


def test_session_middleware_is_the_only_writer_of_request_state_user_id() -> None:
    """Nothing sets ``user_id`` earlier, so the limiter cannot see one another way."""
    writers: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "user_id"
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "state"
                ):
                    writers.add(str(path.relative_to(SRC_ROOT)))
    assert writers == {str(SESSION_MIDDLEWARE.relative_to(SRC_ROOT))}, (
        f"request.state.user_id is now written by {sorted(writers)}. If one of those "
        "runs outside RateLimitMiddleware, the limiter is no longer inert for that "
        "traffic and the assertions in this module need re-deriving."
    )


@patch("api.middleware.rate_limit.increment_counter", new_callable=AsyncMock)
def test_cookie_session_request_is_never_rate_limited(mock_increment, monkeypatch) -> None:
    """End to end: the session is valid at the route, but the limiter saw nobody."""
    monkeypatch.setattr(api_main, "_session_middleware_manager", _StubSessionManager())
    app, seen_user_ids = _replica_stack()

    with TestClient(app, cookies={"kagura_session": "valid-session-id"}) as client:
        response = client.get(PROBE_PATH)

    # The route DID get an authenticated request...
    assert response.status_code == 200
    assert response.json() == {"user_id": SESSION_USER["user_id"]}
    # ...but the limiter, running outside the session middleware, saw no user.
    assert seen_user_ids == [None]
    # Early return: no counter touched, no X-RateLimit-* headers on the response.
    mock_increment.assert_not_awaited()
    assert "X-RateLimit-Limit" not in response.headers
    assert "X-RateLimit-Remaining" not in response.headers


@patch("api.middleware.rate_limit.increment_counter", new_callable=AsyncMock)
def test_free_plan_rest_refusal_is_unreachable_through_the_stack(
    mock_increment, monkeypatch
) -> None:
    """The Free-tier REST refusal cannot fire for a cookie session.

    ``rest_calls_per_day`` is 0 on Free, so ``_check_daily_quota`` would raise
    ``QuotaExceededError`` → 429 ``QUOTA-001``. Through the real middleware
    order it never gets that far.
    """
    monkeypatch.setattr(api_main, "_session_middleware_manager", _StubSessionManager())
    mock_increment.return_value = 1
    app, _seen = _replica_stack()

    with patch.object(
        RateLimitMiddleware, "_get_user_plan", new=AsyncMock(return_value=("free", None))
    ) as mock_plan:
        with TestClient(app, cookies={"kagura_session": "valid-session-id"}) as client:
            response = client.get(PROBE_PATH)

    assert response.status_code == 200, "a Free cookie session was refused — order changed?"
    assert "QUOTA-001" not in response.text
    mock_plan.assert_not_awaited()


@pytest.mark.asyncio
@patch("api.middleware.rate_limit.increment_counter", new_callable=AsyncMock)
async def test_the_refusal_itself_still_works_when_user_id_is_already_set(
    mock_increment,
) -> None:
    """Disproving reachability is not disproving the code: the gate is intact.

    Hand the middleware a request that already carries ``user_id`` (what a
    reorder — or a future auth layer that runs outside the limiter — would
    produce) and the Free-plan REST refusal fires exactly as written.
    """
    mock_increment.return_value = 1
    middleware = RateLimitMiddleware(MagicMock())

    request = MagicMock(spec=Request)
    request.url.path = PROBE_PATH
    request.state.user_id = SESSION_USER["user_id"]
    request.state.user = dict(SESSION_USER)

    async def _call_next(_request):  # pragma: no cover - must not be reached
        raise AssertionError("request should have been refused before the route")

    with patch.object(
        RateLimitMiddleware, "_get_user_plan", new=AsyncMock(return_value=("free", None))
    ):
        response = await middleware.dispatch(request, _call_next)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 429
    assert b"QUOTA-001" in response.body
    assert b"REST API is not available on Free plan" in response.body
