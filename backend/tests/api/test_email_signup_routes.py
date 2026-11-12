"""Email sign-up routes (#1734).

The service is covered in ``tests/services/test_email_signup_service.py`` and
against Postgres in ``tests/integration/test_email_signup_db.py``; these
tests pin what the routes add: the kill switch, the uniform request answer,
the limits (and that sending fails closed), the shape-only address check,
no return path in the request, and how completion opens the session.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from api.main import app
from api.routes import email_signup as routes
from auth.roles import Role
from config.settings import get_settings
from services.email_signup_service import CreatedEmailAccount, process_signup_request
from utils.exceptions import RateLimitError, RedisError


def _request(ip: str = "192.0.2.10") -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip), headers={"user-agent": "pytest"})


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_email_signup", True)


@pytest.fixture
def counters(monkeypatch) -> dict[str, int]:
    store: dict[str, int] = {}

    async def _incr(key: str, ttl: int | None = None) -> int:
        store[key] = store.get(key, 0) + 1
        return store[key]

    monkeypatch.setattr(routes, "increment_counter", _incr)
    return store


def _scheduled(tasks: BackgroundTasks) -> list[tuple[object, dict]]:
    return [(task.func, dict(task.kwargs)) for task in tasks.tasks]


async def _ask(email: str, *, invite: str | None = None, ip: str = "192.0.2.10"):
    tasks = BackgroundTasks()
    answer = await routes.request_email_signup(
        routes.EmailSignupRequestBody(
            email=email, invite=invite, locale="ja", timezone="Asia/Tokyo"
        ),
        _request(ip),
        tasks,
    )
    return answer, _scheduled(tasks)


class TestKillSwitch:
    @pytest.mark.parametrize("path", ["request", "preview", "complete"])
    def test_every_route_404s_when_off_even_without_a_body(self, path, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "enable_email_signup", False)
        resp = TestClient(app).post(f"/api/v1/auth/signup/email/{path}")
        assert resp.status_code == 404


class TestRequest:
    @pytest.mark.asyncio
    async def test_same_answer_and_same_task_for_every_address(self, enabled, counters) -> None:
        one, one_tasks = await _ask("Somebody@Example.test")
        two, two_tasks = await _ask("nobody@example.test")
        assert one.model_dump() == two.model_dump()
        assert one.model_dump() == {
            "status": "accepted",
            "message": "If this address can be used to sign up, we sent a link to it.",
        }
        ((func_one, kw_one),) = one_tasks
        ((func_two, kw_two),) = two_tasks
        assert func_one is func_two is process_signup_request
        assert kw_one["email"] == "somebody@example.test"  # normalized
        assert set(kw_one) == {"email", "beta_invite_token_hash", "locale", "timezone"}

    @pytest.mark.asyncio
    async def test_a_valid_invite_travels_as_its_hash_only(
        self, enabled, counters, monkeypatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "enable_beta_invites", True)
        token = "A" * 43
        _, [(_, kwargs)] = await _ask("a@example.test", invite=token)
        assert kwargs["beta_invite_token_hash"] is not None
        assert token not in repr(kwargs)

    @pytest.mark.asyncio
    async def test_invite_is_inert_when_beta_invites_are_off_or_malformed(
        self, enabled, counters, monkeypatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "enable_beta_invites", False)
        _, [(_, off)] = await _ask("a@example.test", invite="A" * 43)
        monkeypatch.setattr(get_settings(), "enable_beta_invites", True)
        _, [(_, bad)] = await _ask("b@example.test", invite="not a token!")
        assert off["beta_invite_token_hash"] is None
        assert bad["beta_invite_token_hash"] is None

    @pytest.mark.asyncio
    async def test_per_ip_limit_is_a_429(self, enabled, counters) -> None:
        for n in range(10):
            await _ask(f"p{n}@example.test")
        with pytest.raises(RateLimitError):
            await _ask("p10@example.test")

    @pytest.mark.asyncio
    async def test_per_address_limit_is_silent(self, enabled, counters) -> None:
        for n in range(3):
            _, tasks = await _ask("same@example.test", ip=f"192.0.2.{n}")
            assert len(tasks) == 1
        answer, tasks = await _ask("same@example.test", ip="192.0.2.99")
        assert answer.status == "accepted"
        assert tasks == []

    @pytest.mark.asyncio
    async def test_sending_fails_closed_when_the_limiter_cannot_count(
        self, enabled, monkeypatch
    ) -> None:
        async def _down(key: str, ttl: int | None = None) -> int:
            raise RedisError("down")

        monkeypatch.setattr(routes, "increment_counter", _down)
        answer, tasks = await _ask("a@example.test")
        assert answer.status == "accepted"
        assert tasks == []

    def test_request_body_has_no_return_path(self) -> None:
        assert "return_to" not in routes.EmailSignupRequestBody.model_fields
        body = routes.EmailSignupRequestBody.model_validate(
            {"email": "a@example.test", "return_to": "/device?user_code=X"}
        )
        assert not hasattr(body, "return_to")

    @pytest.mark.parametrize(
        "address",
        [
            "a@example.test, b@example.test",
            "Name <a@example.test>",
            "a@@example.test",
            "a b@example.test",
            "a@example",
            "a@exämple.test",
            "а@example.test",
            "a@example.test\r\nBcc: x@example.test",
            '"a"@example.test',
            "a;b@example.test",
            "admin@local",
            "a@-example.test",
        ],
    )
    def test_bad_address_shapes_are_a_422(self, address: str) -> None:
        with pytest.raises(PydanticValidationError):
            routes.EmailSignupRequestBody(email=address)

    def test_good_shapes_are_normalized(self) -> None:
        body = routes.EmailSignupRequestBody(email="  First.Last+tag@Sub.Example.CO.jp ")
        assert body.email == "first.last+tag@sub.example.co.jp"

    def test_http_shape_is_identical_for_two_addresses(self, enabled, monkeypatch) -> None:
        async def _incr(key: str, ttl: int | None = None) -> int:
            return 1

        monkeypatch.setattr(routes, "increment_counter", _incr)
        ran: list[dict] = []

        async def _fake_task(**kwargs):
            ran.append(kwargs)

        monkeypatch.setattr(routes, "process_signup_request", _fake_task)
        client = TestClient(app)
        a = client.post("/api/v1/auth/signup/email/request", json={"email": "x@example.test"})
        b = client.post("/api/v1/auth/signup/email/request", json={"email": "y@example.test"})
        assert a.status_code == b.status_code == 202
        assert a.content == b.content
        skip = {"date", "x-request-id", "x-response-time", "x-process-time"}
        assert {k: v for k, v in a.headers.items() if k.lower() not in skip} == {
            k: v for k, v in b.headers.items() if k.lower() not in skip
        }
        assert [r["email"] for r in ran] == ["x@example.test", "y@example.test"]


class TestComplete:
    @pytest.fixture
    def created(self, monkeypatch, counters):
        account = CreatedEmailAccount(
            user_id="email:abc",
            email="a@example.test",
            name="A",
            role=Role.USER,
            admission="open_registration",
        )
        service = MagicMock()
        service.complete = AsyncMock(return_value=account)
        monkeypatch.setattr(routes, "EmailSignupService", MagicMock(return_value=service))
        workspace = AsyncMock()
        monkeypatch.setattr(routes, "ensure_first_workspace", workspace)
        note = AsyncMock()
        monkeypatch.setattr(routes, "note_browser_sign_in", note)
        return SimpleNamespace(service=service, workspace=workspace, note=note)

    @pytest.mark.asyncio
    async def test_opens_a_session_with_the_new_role_and_sanitizes_return_to(
        self, enabled, created, monkeypatch
    ) -> None:
        create_session = AsyncMock(return_value="sess-1")
        monkeypatch.setattr(routes.auth_module, "_create_password_session", create_session)
        cookie = MagicMock()
        monkeypatch.setattr(routes.auth_module, "_set_session_cookie", cookie)
        resp = await routes.complete_email_signup(
            routes.EmailSignupCompleteBody(
                token="t", new_password="pw", return_to="https://evil.example/x", name=" A\n B "
            ),
            _request(),
            db=MagicMock(),
        )
        assert resp.status_code == 200
        assert b"evil.example" not in resp.body
        create_session.assert_awaited_once_with(
            user_id="email:abc", email="a@example.test", name="A", role="user"
        )
        cookie.assert_called_once_with(resp, "sess-1")
        created.workspace.assert_awaited_once()
        assert created.note.await_args.kwargs["sign_in_method"] == "Password"
        assert created.service.complete.await_args.kwargs["name"] == "A B"

    @pytest.mark.asyncio
    async def test_session_store_failure_after_commit_is_a_503(
        self, enabled, created, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            routes.auth_module,
            "_create_password_session",
            AsyncMock(side_effect=RuntimeError("redis down")),
        )
        with pytest.raises(HTTPException) as info:
            await routes.complete_email_signup(
                routes.EmailSignupCompleteBody(token="t", new_password="pw"),
                _request(),
                db=MagicMock(),
            )
        assert info.value.status_code == 503
        assert "account was created" in info.value.detail

    @pytest.mark.asyncio
    async def test_link_attempts_are_limited_per_ip(self, enabled, counters) -> None:
        for _ in range(20):
            await routes._link_attempt(_request())
        with pytest.raises(RateLimitError):
            await routes._link_attempt(_request())


class TestPreview:
    @pytest.mark.asyncio
    async def test_returns_address_and_aware_expiry(self, enabled, counters, monkeypatch) -> None:
        service = MagicMock()
        service.preview = AsyncMock(return_value=("a@example.test", datetime(2026, 1, 1, 0, 0)))
        monkeypatch.setattr(routes, "EmailSignupService", MagicMock(return_value=service))
        answer = await routes.preview_email_signup(
            routes.EmailSignupTokenBody(token="t"), _request(), db=MagicMock()
        )
        dumped = answer.model_dump(mode="json")
        assert dumped["email"] == "a@example.test"
        assert dumped["expires_at"].endswith(("Z", "+00:00"))
