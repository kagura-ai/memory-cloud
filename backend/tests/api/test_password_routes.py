"""Self-service password routes (Issue #1678).

The service is covered against real Postgres in
``tests/services/test_password_account_service.py``; these tests pin what the
routes add: the uniform reset-request answer, the rate limits, which sessions
each flow revokes, and the auth dependency of each route.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks

from api.main import app
from api.routes import password as password_routes
from auth.dependencies import require_session_auth
from services.password_account_service import PendingResetEmail
from utils.exceptions import RateLimitError, RedisError


def _request(ip: str = "192.0.2.10", cookie: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host=ip),
        headers={"user-agent": "pytest"},
        cookies={"kagura_session": cookie} if cookie else {},
    )


@pytest.fixture
def counters(monkeypatch) -> dict[str, int]:
    store: dict[str, int] = {}

    async def _incr(key: str, ttl: int | None = None) -> int:
        store[key] = store.get(key, 0) + 1
        return store[key]

    monkeypatch.setattr(password_routes, "increment_counter", _incr)
    return store


@pytest.fixture
def service(monkeypatch) -> MagicMock:
    instance = MagicMock()
    instance.request_reset = AsyncMock(return_value=None)
    instance.send_reset_email = AsyncMock()
    instance.complete_reset = AsyncMock(return_value="u-1")
    instance.complete_setup = AsyncMock(return_value="u-1")
    instance.request_setup = AsyncMock()
    instance.change = AsyncMock()
    instance.remove = AsyncMock()
    monkeypatch.setattr(password_routes, "PasswordAccountService", MagicMock(return_value=instance))
    return instance


@pytest.fixture
def sessions(monkeypatch) -> MagicMock:
    manager = MagicMock()
    manager.delete_user_sessions = MagicMock(return_value=2)
    monkeypatch.setattr(password_routes.auth_module, "_session_manager", manager)
    return manager


_PENDING = PendingResetEmail(
    to_email="p@example.test", reset_url="https://x/password/reset?token=t", expires_in_minutes=30
)


class TestResetRequest:
    @pytest.mark.asyncio
    async def test_same_answer_whether_or_not_an_account_matched(self, counters, service) -> None:
        body = password_routes.PasswordResetRequestBody(email="a@example.test")

        miss_tasks = BackgroundTasks()
        miss = await password_routes.request_password_reset(body, _request(), miss_tasks, db=None)

        service.request_reset.return_value = _PENDING
        hit_tasks = BackgroundTasks()
        hit = await password_routes.request_password_reset(
            password_routes.PasswordResetRequestBody(email="b@example.test"),
            _request(),
            hit_tasks,
            db=None,
        )

        assert miss.model_dump() == hit.model_dump()
        assert miss_tasks.tasks == []
        assert len(hit_tasks.tasks) == 1  # the email goes out after the response

    @pytest.mark.asyncio
    async def test_email_is_normalized(self, counters, service) -> None:
        body = password_routes.PasswordResetRequestBody(email="  Person@Example.TEST ")
        await password_routes.request_password_reset(body, _request(), BackgroundTasks(), db=None)
        assert service.request_reset.await_args.kwargs["email"] == "person@example.test"

    @pytest.mark.asyncio
    async def test_per_ip_limit_is_a_429(self, counters, service) -> None:
        for i in range(password_routes._RESET_REQUESTS_PER_IP):
            body = password_routes.PasswordResetRequestBody(email=f"u{i}@example.test")
            await password_routes.request_password_reset(
                body, _request(), BackgroundTasks(), db=None
            )
        with pytest.raises(RateLimitError):
            await password_routes.request_password_reset(
                password_routes.PasswordResetRequestBody(email="z@example.test"),
                _request(),
                BackgroundTasks(),
                db=None,
            )

    @pytest.mark.asyncio
    async def test_per_email_limit_is_silent(self, counters, service) -> None:
        service.request_reset.return_value = _PENDING
        answers = []
        for i in range(password_routes._RESET_REQUESTS_PER_EMAIL + 2):
            answers.append(
                await password_routes.request_password_reset(
                    password_routes.PasswordResetRequestBody(email="same@example.test"),
                    _request(ip=f"192.0.2.{i}"),
                    BackgroundTasks(),
                    db=None,
                )
            )
        assert {a.status for a in answers} == {"accepted"}
        assert service.request_reset.await_count == password_routes._RESET_REQUESTS_PER_EMAIL

    @pytest.mark.asyncio
    async def test_redis_outage_fails_open(self, monkeypatch, service) -> None:
        monkeypatch.setattr(
            password_routes, "increment_counter", AsyncMock(side_effect=RedisError("down"))
        )
        answer = await password_routes.request_password_reset(
            password_routes.PasswordResetRequestBody(email="a@example.test"),
            _request(),
            BackgroundTasks(),
            db=None,
        )
        assert answer.status == "accepted"


class TestSessionRevocation:
    @pytest.mark.asyncio
    async def test_reset_revokes_every_session(self, counters, service, sessions) -> None:
        body = password_routes.PasswordLinkBody(token="t" * 43, new_password="x")
        response = await password_routes.reset_password(body, _request(cookie="mine"), db=None)
        assert response.status_code == 204
        sessions.delete_user_sessions.assert_called_once_with("u-1", exclude_session_id=None)

    @pytest.mark.asyncio
    async def test_setup_keeps_this_browser(self, counters, service, sessions) -> None:
        body = password_routes.PasswordLinkBody(token="t" * 43, new_password="x")
        await password_routes.setup_password(body, _request(cookie="mine"), db=None)
        sessions.delete_user_sessions.assert_called_once_with("u-1", exclude_session_id="mine")

    @pytest.mark.asyncio
    async def test_change_keeps_the_current_session(self, counters, service, sessions) -> None:
        body = password_routes.PasswordChangeBody(current_password="a", new_password="b")
        response = await password_routes.change_password(
            body, _request(cookie="current"), {"user_id": "u-1"}, db=None
        )
        assert response.status_code == 204
        sessions.delete_user_sessions.assert_called_once_with("u-1", exclude_session_id="current")

    @pytest.mark.asyncio
    async def test_remove_keeps_the_current_session(self, counters, service, sessions) -> None:
        body = password_routes.PasswordRemoveBody(current_password="a")
        await password_routes.remove_password(
            body, _request(cookie="current"), {"user_id": "u-1"}, db=None
        )
        sessions.delete_user_sessions.assert_called_once_with("u-1", exclude_session_id="current")

    @pytest.mark.asyncio
    async def test_failed_change_revokes_nothing(self, counters, service, sessions) -> None:
        service.change.side_effect = RuntimeError("wrong password")
        body = password_routes.PasswordChangeBody(current_password="a", new_password="b")
        with pytest.raises(RuntimeError):
            await password_routes.change_password(
                body, _request(cookie="current"), {"user_id": "u-1"}, db=None
            )
        sessions.delete_user_sessions.assert_not_called()


class TestLimits:
    @pytest.mark.asyncio
    async def test_current_password_guesses_are_limited(self, counters, service, sessions) -> None:
        body = password_routes.PasswordChangeBody(current_password="a", new_password="b")
        for _ in range(password_routes._CURRENT_PASSWORD_ATTEMPTS_PER_USER):
            await password_routes.change_password(body, _request(), {"user_id": "u-1"}, db=None)
        with pytest.raises(RateLimitError):
            await password_routes.remove_password(
                password_routes.PasswordRemoveBody(current_password="a"),
                _request(),
                {"user_id": "u-1"},
                db=None,
            )

    @pytest.mark.asyncio
    async def test_setup_requests_are_limited(self, counters, service) -> None:
        for _ in range(password_routes._SETUP_REQUESTS_PER_USER):
            answer = await password_routes.request_password_setup(
                _request(), {"user_id": "u-1"}, db=None
            )
            assert answer.status == "sent"
        with pytest.raises(RateLimitError):
            await password_routes.request_password_setup(_request(), {"user_id": "u-1"}, db=None)


class TestRouteWiring:
    """Public routes carry no auth dependency; /me routes are session-only."""

    @staticmethod
    def _route(path: str, method: str):
        for router in (password_routes.router, password_routes.me_router):
            for route in router.routes:
                if "/api/v1" + route.path == path and method in route.methods:
                    return route
        raise AssertionError(f"{method} {path} is not registered")

    def test_routers_are_mounted(self) -> None:
        paths = app.openapi()["paths"]
        assert "post" in paths["/api/v1/auth/password/reset-request"]
        assert "delete" in paths["/api/v1/me/password"]

    @staticmethod
    def _dependency_calls(route) -> set:
        calls = set()
        stack = list(route.dependant.dependencies)
        while stack:
            dep = stack.pop()
            calls.add(dep.call)
            stack.extend(dep.dependencies)
        return calls

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/api/v1/me/password/setup-request", "POST"),
            ("/api/v1/me/password/change", "POST"),
            ("/api/v1/me/password", "DELETE"),
        ],
    )
    def test_me_routes_require_a_browser_session(self, path: str, method: str) -> None:
        assert require_session_auth in self._dependency_calls(self._route(path, method))

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/auth/password/reset-request",
            "/api/v1/auth/password/reset",
            "/api/v1/auth/password/setup",
        ],
    )
    def test_public_routes(self, path: str) -> None:
        assert require_session_auth not in self._dependency_calls(self._route(path, "POST"))
