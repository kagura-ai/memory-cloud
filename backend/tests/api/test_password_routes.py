"""Self-service password routes (Issue #1678).

The service is covered against real Postgres in
``tests/services/test_password_account_service.py``; these tests pin what the
routes add: the uniform reset-request answer, the rate limits, which sessions
each flow revokes, and the auth dependency of each route.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks

from api.main import app
from api.routes import password as password_routes
from auth.dependencies import require_session_auth
from db.base import get_db
from services import password_account_service as password_service_module
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


@pytest.fixture
def session_factory(monkeypatch) -> MagicMock:
    """Stand-in for the background task's own session factory.

    The request path must never touch it: the lookup, the token and the audit
    row all happen after the response.
    """
    factory = MagicMock()
    monkeypatch.setattr(password_service_module, "_get_session_factory", factory)
    return factory


def _scheduled(tasks: BackgroundTasks) -> list[tuple[object, dict]]:
    return [(task.func, dict(task.kwargs)) for task in tasks.tasks]


class TestResetRequest:
    @pytest.mark.asyncio
    async def test_same_answer_and_same_work_whether_or_not_an_account_matched(
        self, counters, service, session_factory
    ) -> None:
        # One address names an account and one does not; the request path
        # cannot tell them apart because it never looks.
        miss_tasks = BackgroundTasks()
        miss = await password_routes.request_password_reset(
            password_routes.PasswordResetRequestBody(email="nobody@example.test"),
            _request(),
            miss_tasks,
        )
        hit_tasks = BackgroundTasks()
        hit = await password_routes.request_password_reset(
            password_routes.PasswordResetRequestBody(email="somebody@example.test"),
            _request(),
            hit_tasks,
        )

        assert miss.model_dump() == hit.model_dump()
        ((miss_func, miss_kwargs),) = _scheduled(miss_tasks)
        ((hit_func, hit_kwargs),) = _scheduled(hit_tasks)
        assert miss_func is hit_func is password_routes.process_reset_request
        assert miss_kwargs["email"] == "nobody@example.test"
        assert hit_kwargs["email"] == "somebody@example.test"
        del miss_kwargs["email"], hit_kwargs["email"]
        assert miss_kwargs == hit_kwargs
        # No lookup, token, audit row or commit on the request path.
        session_factory.assert_not_called()
        service.request_reset.assert_not_awaited()

    def test_request_path_takes_no_db_session(self) -> None:
        assert "db" not in inspect.signature(password_routes.request_password_reset).parameters
        route = TestRouteWiring._route("/api/v1/auth/password/reset-request", "POST")
        assert get_db not in TestRouteWiring._dependency_calls(route)

    @pytest.mark.asyncio
    async def test_email_is_normalized(self, counters, session_factory) -> None:
        tasks = BackgroundTasks()
        body = password_routes.PasswordResetRequestBody(email="  Person@Example.TEST ")
        await password_routes.request_password_reset(body, _request(), tasks)
        ((_, kwargs),) = _scheduled(tasks)
        assert kwargs["email"] == "person@example.test"
        assert kwargs["ip_address"] == "192.0.2.10"
        assert kwargs["user_agent"] == "pytest"

    @pytest.mark.asyncio
    async def test_per_ip_limit_is_a_429(self, counters, session_factory) -> None:
        for i in range(password_routes._RESET_REQUESTS_PER_IP):
            body = password_routes.PasswordResetRequestBody(email=f"u{i}@example.test")
            await password_routes.request_password_reset(body, _request(), BackgroundTasks())
        with pytest.raises(RateLimitError):
            await password_routes.request_password_reset(
                password_routes.PasswordResetRequestBody(email="z@example.test"),
                _request(),
                BackgroundTasks(),
            )

    @pytest.mark.asyncio
    async def test_per_email_limit_is_silent(self, counters, session_factory) -> None:
        answers = []
        scheduled = 0
        for i in range(password_routes._RESET_REQUESTS_PER_EMAIL + 2):
            tasks = BackgroundTasks()
            answers.append(
                await password_routes.request_password_reset(
                    password_routes.PasswordResetRequestBody(email="same@example.test"),
                    _request(ip=f"192.0.2.{i}"),
                    tasks,
                )
            )
            scheduled += len(tasks.tasks)
        assert {a.status for a in answers} == {"accepted"}
        assert scheduled == password_routes._RESET_REQUESTS_PER_EMAIL

    @pytest.mark.asyncio
    async def test_redis_outage_fails_open(self, monkeypatch, session_factory) -> None:
        monkeypatch.setattr(
            password_routes, "increment_counter", AsyncMock(side_effect=RedisError("down"))
        )
        answer = await password_routes.request_password_reset(
            password_routes.PasswordResetRequestBody(email="a@example.test"),
            _request(),
            BackgroundTasks(),
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
