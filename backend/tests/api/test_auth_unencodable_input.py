"""Auth input that cannot be UTF-8 encoded (Issue #1718).

``"\\ud800"`` is a legal JSON escape. Starlette's ``request.json()`` turns it
into a ``str`` holding a lone surrogate, plain ``str`` fields accept it, and
``str.encode()`` then raised ``UnicodeEncodeError`` — from bcrypt, pyotp or a
Redis key — so each request below used to end in a bare 500. Each one must get
the answer a wrong credential gets.

The requests go through ``api.main.app`` with the raw JSON escape in the body:
httpx cannot encode a lone surrogate from ``json=``. Redis is ``fakeredis``,
which encodes keys with redis-py's own strict encoder, so a surrogate that
reached a key would still raise here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import bcrypt
import fakeredis
import fakeredis.aioredis
import pyotp
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import auth as auth_routes
from auth.dependencies import require_session_auth
from db.base import get_db
from models.erasure import ErasureRequest
from services import account_erasure_service as erasure_module
from services.account_erasure_service import (
    REASON_SELF_SERVICE,
    STATUS_PENDING,
    AccountErasureService,
    _sha256_hex,
)

JSON = {"Content-Type": "application/json"}
PASSWORD = "Correct-Horse-1!"
TOTP_SECRET = pyotp.random_base32()


def _raw(body: str) -> bytes:
    """Encode a JSON text whose ``\\uXXXX`` escapes stay escapes on the wire."""
    return body.encode("ascii")


@pytest.fixture
def redis(monkeypatch) -> fakeredis.FakeRedis:
    fake = fakeredis.FakeRedis(decode_responses=True)
    manager = MagicMock()
    manager._redis = fake
    monkeypatch.setattr(auth_routes, "_session_manager", manager)
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _password_user(monkeypatch, *, totp: bool = False) -> MagicMock:
    user = SimpleNamespace(
        user_id="user-1",
        email="user@example.test",
        name="User",
        role="user",
        password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
        totp_enabled=totp,
        totp_secret="encrypted" if totp else None,
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=user)))

    async def _fake_db():
        yield db

    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    monkeypatch.setattr(
        auth_routes, "get_encryptor", lambda: SimpleNamespace(decrypt=lambda _v: TOTP_SECRET)
    )
    return db


class TestPasswordLogin:
    def test_wrong_password_baseline(self, client, redis, monkeypatch) -> None:
        _password_user(monkeypatch)
        response = client.post(
            "/api/v1/auth/login", content=_raw('{"login_id":"x","password":"wrong"}'), headers=JSON
        )
        assert response.status_code == 401
        assert response.json()["error"] == "AUTH-002"
        assert redis.get("login_attempts:x") == "1"

    def test_unencodable_password_is_401_and_counted(self, client, redis, monkeypatch) -> None:
        _password_user(monkeypatch)
        response = client.post(
            "/api/v1/auth/login",
            content=_raw('{"login_id":"x","password":"\\ud800"}'),
            headers=JSON,
        )
        assert response.status_code == 401
        assert response.json()["error"] == "AUTH-002"
        assert redis.get("login_attempts:x") == "1"

    def test_unencodable_passwords_hit_the_rate_limit(self, client, redis, monkeypatch) -> None:
        _password_user(monkeypatch)
        body = _raw('{"login_id":"x","password":"Abc\\udc00"}')
        for _ in range(auth_routes._MAX_LOGIN_ATTEMPTS):
            assert client.post("/api/v1/auth/login", content=body, headers=JSON).status_code == 401
        assert client.post("/api/v1/auth/login", content=body, headers=JSON).status_code == 429

    def test_unencodable_login_id_is_401_without_redis_or_db(
        self, client, redis, monkeypatch
    ) -> None:
        db = _password_user(monkeypatch)
        response = client.post(
            "/api/v1/auth/login",
            content=_raw('{"login_id":"\\udc00","password":"wrong"}'),
            headers=JSON,
        )
        assert response.status_code == 401
        assert response.json()["error"] == "AUTH-002"
        db.execute.assert_not_called()
        assert redis.keys("*") == []


class TestMfaVerify:
    def _pending(self, redis) -> str:
        token = "pending-token"
        redis.setex(f"mfa_pending:{token}", 300, "user-1")
        return token

    def test_unencodable_code_is_a_wrong_code(self, client, redis, monkeypatch) -> None:
        _password_user(monkeypatch, totp=True)
        token = self._pending(redis)
        response = client.post(
            "/api/v1/auth/mfa/verify",
            content=_raw(f'{{"mfa_session_token":"{token}","totp_code":"12345\\ud800"}}'),
            headers=JSON,
        )
        assert response.status_code == 401
        assert "Invalid TOTP code" in response.json()["message"]
        # Deleted like any wrong code, so the pending step cannot be replayed.
        assert redis.get(f"mfa_pending:{token}") is None

    def test_unencodable_session_token_is_an_invalid_session(
        self, client, redis, monkeypatch
    ) -> None:
        db = _password_user(monkeypatch, totp=True)
        token = self._pending(redis)
        response = client.post(
            "/api/v1/auth/mfa/verify",
            content=_raw('{"mfa_session_token":"\\ud800","totp_code":"123456"}'),
            headers=JSON,
        )
        assert response.status_code == 401
        assert "Invalid or expired MFA session" in response.json()["message"]
        db.execute.assert_not_called()
        assert redis.get(f"mfa_pending:{token}") == "user-1"


class TestErasureConfirm:
    TOKEN = "erasure-token"

    @pytest.fixture
    def erasure(self, monkeypatch):
        target = SimpleNamespace(
            user_id="user-1",
            email="user@example.test",
            auth_method="password",
            password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
        )
        request = ErasureRequest(
            user_id="user-1",
            user_email_hash="x",
            initiated_by="user-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(self.TOKEN),
        )
        request.id = uuid4()
        server = fakeredis.FakeServer()
        # The service's async client, and a sync one on the same data for setup.
        fake = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        monkeypatch.setattr(erasure_module, "get_redis_client", lambda: fake)
        sync = fakeredis.FakeRedis(server=server, decode_responses=True)
        monkeypatch.setattr(
            AccountErasureService, "_load_user_or_404", AsyncMock(return_value=target)
        )
        monkeypatch.setattr(
            AccountErasureService, "_load_request_or_404", AsyncMock(return_value=request)
        )

        async def _db():
            yield MagicMock()

        app.dependency_overrides[require_session_auth] = lambda: {"user_id": "user-1"}
        app.dependency_overrides[get_db] = _db
        yield SimpleNamespace(redis=sync, request=request)
        app.dependency_overrides.pop(require_session_auth, None)
        app.dependency_overrides.pop(get_db, None)

    def _post(self, client, body: str):
        return client.post("/api/v1/me/account/erasure-confirm", content=_raw(body), headers=JSON)

    def test_unencodable_password_is_incorrect_password(self, client, erasure) -> None:
        erasure.redis.set(f"erasure_token:{self.TOKEN}", str(erasure.request.id))
        response = self._post(client, f'{{"token":"{self.TOKEN}","password":"\\ud800"}}')
        assert response.status_code == 403
        assert response.json()["error"] == "ERASURE-003"
        assert erasure.request.status == STATUS_PENDING

    def test_unencodable_token_is_an_invalid_token(self, client, erasure) -> None:
        response = self._post(client, f'{{"token":"\\ud800","password":"{PASSWORD}"}}')
        assert response.status_code == 400
        assert response.json()["error"] == "ERASURE-002"
        assert erasure.request.status == STATUS_PENDING
