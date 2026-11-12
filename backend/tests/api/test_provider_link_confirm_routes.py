"""Provider link confirmation routes (#2786).

The service is covered in ``tests/services/test_provider_link_confirmation_service.py``
and against Postgres in ``tests/integration/test_provider_link_confirm_db.py``.
These tests pin what the routes add: the switch, the limits, the answers and
how completion opens the session (no proof, the password sign-in sweep, the
browser nonce cookie and marker dropped, the new-device note).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from api.routes import provider_link_confirm as routes
from auth.roles import Role
from config.settings import get_settings
from services.provider_link_confirmation_service import (
    ConfirmedProviderLink,
    ProviderLinkPreview,
)
from utils.exceptions import (
    PasswordLinkInvalidError,
    ProviderLinkWrongBrowserError,
    RateLimitError,
    RedisError,
)
from utils.hashing import sha256_hex

NONCE = "N" * 43
TOKEN = "T" * 43


def _request(*, cookie: str | None = NONCE, ip: str = "192.0.2.10") -> SimpleNamespace:
    cookies = {"kagura_oauth_browser": cookie} if cookie else {}
    return SimpleNamespace(
        client=SimpleNamespace(host=ip), headers={"user-agent": "pytest"}, cookies=cookies
    )


@pytest.fixture
def counters(monkeypatch) -> dict[str, int]:
    store: dict[str, int] = {}

    async def _incr(key: str, ttl: int | None = None) -> int:
        store[key] = store.get(key, 0) + 1
        return store[key]

    monkeypatch.setattr(routes, "increment_counter", _incr)
    return store


def _service(monkeypatch, **methods) -> MagicMock:
    service = MagicMock()
    for name, value in methods.items():
        setattr(service, name, value)
    factory = MagicMock(return_value=service)
    monkeypatch.setattr(routes, "ProviderLinkConfirmationService", factory)
    return service


class TestSwitch:
    @pytest.mark.parametrize("path", ["preview", "complete", "cancel"])
    def test_every_route_404s_when_off_even_without_a_body(self, path, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "enable_provider_link_confirmation", False)
        resp = TestClient(app).post(f"/api/v1/auth/link-confirm/{path}")
        assert resp.status_code == 404

    @pytest.mark.parametrize("path", ["preview", "complete", "cancel"])
    def test_routes_exist_when_on(self, path, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "enable_provider_link_confirmation", True)
        resp = TestClient(app).post(f"/api/v1/auth/link-confirm/{path}")
        assert resp.status_code == 422


class TestErrors:
    @pytest.mark.parametrize(
        ("exc", "status", "code"),
        [
            (ProviderLinkWrongBrowserError(), 403, "AUTH-310"),
            (PasswordLinkInvalidError(), 400, "AUTH-301"),
        ],
    )
    def test_complete_errors_keep_their_codes(self, exc, status, code, monkeypatch) -> None:
        monkeypatch.setattr(routes, "increment_counter", AsyncMock(return_value=1))
        _service(monkeypatch, complete=AsyncMock(side_effect=exc))
        resp = TestClient(app).post("/api/v1/auth/link-confirm/complete", json={"token": TOKEN})
        assert resp.status_code == status
        assert resp.json()["error"] == code
        assert "set-cookie" not in {k.lower() for k in resp.headers}

    def test_token_length_is_bounded(self) -> None:
        resp = TestClient(app).post("/api/v1/auth/link-confirm/preview", json={"token": "x" * 129})
        assert resp.status_code == 422


class TestPreview:
    @pytest.mark.asyncio
    async def test_shape_and_aware_expiry(self, counters, monkeypatch) -> None:
        preview = ProviderLinkPreview(
            provider="github",
            provider_login="octo-cat",
            provider_name="Octo",
            provider_avatar_url=None,
            expires_at=datetime(2026, 1, 1, 0, 15),
            same_browser=True,
        )
        service = _service(monkeypatch, preview=AsyncMock(return_value=preview))
        answer = await routes.preview_provider_link(
            routes.ProviderLinkTokenBody(token=TOKEN), _request(), db=MagicMock()
        )
        dumped = answer.model_dump(mode="json")
        assert dumped["provider"] == "github"
        assert dumped["provider_login"] == "octo-cat"
        assert dumped["same_browser"] is True
        assert dumped["expires_at"].endswith(("Z", "+00:00"))
        assert service.preview.await_args.kwargs == {"raw_token": TOKEN, "browser_nonce": NONCE}

    @pytest.mark.asyncio
    async def test_a_malformed_cookie_is_no_nonce(self, counters, monkeypatch) -> None:
        service = _service(monkeypatch, preview=AsyncMock(side_effect=PasswordLinkInvalidError()))
        with pytest.raises(PasswordLinkInvalidError):
            await routes.preview_provider_link(
                routes.ProviderLinkTokenBody(token=TOKEN), _request(cookie="bad"), db=MagicMock()
            )
        assert service.preview.await_args.kwargs["browser_nonce"] is None


class TestComplete:
    @pytest.fixture
    def confirmed(self, monkeypatch, counters):
        link = ConfirmedProviderLink(
            user_id="email:abc",
            email="a@example.test",
            name="A",
            picture=None,
            role=Role.USER,
            provider="github",
            oauth_sub="583231",
            provider_login="octo-cat",
            return_to="https://evil.example/x",
        )
        service = _service(monkeypatch, complete=AsyncMock(return_value=link))
        manager = MagicMock()
        manager.create_session.return_value = "sess-1"
        manager.session_ttl = 3600
        monkeypatch.setattr(routes.auth_module, "_session_manager", manager)
        sweep = AsyncMock(return_value=True)
        monkeypatch.setattr(routes.auth_module, "_complete_password_sign_in", sweep)
        note = AsyncMock()
        monkeypatch.setattr(routes, "note_browser_sign_in", note)
        redis = MagicMock()
        redis.delete = AsyncMock()
        monkeypatch.setattr(routes, "get_redis_client", lambda: redis)
        return SimpleNamespace(
            service=service, manager=manager, sweep=sweep, note=note, redis=redis
        )

    @pytest.mark.asyncio
    async def test_opens_a_session_without_proof_and_drops_the_nonce(self, confirmed) -> None:
        resp = await routes.complete_provider_link(
            routes.ProviderLinkTokenBody(token=TOKEN), _request(), db=MagicMock()
        )

        assert resp.status_code == 200
        assert b"evil.example" not in resp.body
        data, kwargs = confirmed.manager.create_session.call_args
        assert kwargs == {"proven_at": None}
        assert data[0]["user_id"] == "email:abc" and data[0]["role"] == "user"
        confirmed.sweep.assert_awaited_once_with("email:abc", "a@example.test", "sess-1")
        cookies = [v.decode() for k, v in resp.raw_headers if k.lower() == b"set-cookie"]
        assert any(c.startswith("kagura_session=sess-1") for c in cookies)
        dropped = [c for c in cookies if c.startswith("kagura_oauth_browser=")]
        assert len(dropped) == 1 and "max-age=0" in dropped[0].lower()
        confirmed.redis.delete.assert_awaited_once_with(f"oauth_browser:{sha256_hex(NONCE)}")
        assert confirmed.note.await_args.kwargs["sign_in_method"] == "GitHub"
        assert confirmed.service.complete.await_args.kwargs["browser_nonce"] == NONCE

    @pytest.mark.parametrize("outcome", [False, RuntimeError("redis down"), TimeoutError()])
    @pytest.mark.asyncio
    async def test_a_session_that_cannot_be_opened_is_a_503(self, confirmed, outcome) -> None:
        if isinstance(outcome, BaseException):
            confirmed.sweep.side_effect = outcome
        else:
            confirmed.sweep.return_value = outcome
        with pytest.raises(HTTPException) as info:
            await routes.complete_provider_link(
                routes.ProviderLinkTokenBody(token=TOKEN), _request(), db=MagicMock()
            )
        assert info.value.status_code == 503
        assert "GitHub was connected" in info.value.detail

    @pytest.mark.asyncio
    async def test_attempts_are_limited_per_ip(self, counters) -> None:
        for _ in range(20):
            await routes._attempt(_request())
        with pytest.raises(RateLimitError):
            await routes._attempt(_request())

    @pytest.mark.asyncio
    async def test_the_limiter_fails_open(self, monkeypatch) -> None:
        monkeypatch.setattr(routes, "increment_counter", AsyncMock(side_effect=RedisError("x")))
        await routes._attempt(_request())


class TestCancel:
    def test_always_204(self, monkeypatch) -> None:
        monkeypatch.setattr(routes, "increment_counter", AsyncMock(return_value=1))
        service = _service(monkeypatch, cancel=AsyncMock(return_value=None))
        resp = TestClient(app).post("/api/v1/auth/link-confirm/cancel", json={"token": TOKEN})
        assert resp.status_code == 204
        assert service.cancel.await_args.kwargs["raw_token"] == TOKEN
