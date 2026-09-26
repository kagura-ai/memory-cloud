"""Password login with a password longer than 72 bytes (Issue #1707).

Under bcrypt 5 an over-long password used to raise from ``checkpw`` and answer
500 before ``_record_login_failure`` ran. It must be an ordinary 401 that counts
toward the login rate limit, and a bcrypt-4-era hash made from the first 72
bytes of a long password must still sign in.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bcrypt
import pytest

from api.routes import auth as auth_routes
from utils.exceptions import InvalidCredentialsError

LONG_PASSWORD = "Aa1!" + "x" * 96  # 100 bytes


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def get(self, key: str):
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    def setex(self, key: str, ttl: int, value) -> None:
        self.store[key] = value

    def pipeline(self):
        redis = self
        pipe = MagicMock()
        pipe.incr = MagicMock(
            side_effect=lambda key: redis.store.__setitem__(key, int(redis.store.get(key, 0)) + 1)
        )
        pipe.expire = MagicMock()
        pipe.execute = MagicMock()
        return pipe


class FakeRequest:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.headers = {"user-agent": "pytest"}
        self.client = MagicMock(host="203.0.113.7")


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    manager = MagicMock()
    manager._redis = FakeRedis()
    monkeypatch.setattr(auth_routes, "_session_manager", manager)
    return manager._redis


def _password_user(monkeypatch, password_hash: str) -> SimpleNamespace:
    user = SimpleNamespace(
        user_id="admin-1",
        email="admin@example.test",
        name="Admin",
        role="admin",
        password_hash=password_hash,
        totp_enabled=False,
        totp_secret=None,
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=user)))

    async def _fake_db():
        yield db

    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    monkeypatch.setattr(
        auth_routes, "_create_session_and_workspace", AsyncMock(return_value="sess-1")
    )
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    return user


def _hash(raw: bytes) -> str:
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=4)).decode()


KEY = "login_attempts:admin"


@pytest.mark.asyncio
async def test_over_long_wrong_password_is_401_and_counted(redis, monkeypatch) -> None:
    _password_user(monkeypatch, _hash(b"Correct-Horse-1!"))
    body = auth_routes.PasswordLoginRequest(login_id="admin", password=LONG_PASSWORD)

    with pytest.raises(InvalidCredentialsError):
        await auth_routes.password_login(body, FakeRequest(), return_to=None)

    assert redis.store[KEY] == 1


@pytest.mark.asyncio
async def test_repeated_over_long_passwords_hit_the_rate_limit(redis, monkeypatch) -> None:
    _password_user(monkeypatch, _hash(b"Correct-Horse-1!"))
    body = auth_routes.PasswordLoginRequest(login_id="admin", password=LONG_PASSWORD)

    for _ in range(auth_routes._MAX_LOGIN_ATTEMPTS):
        with pytest.raises(InvalidCredentialsError):
            await auth_routes.password_login(body, FakeRequest(), return_to=None)

    with pytest.raises(auth_routes.HTTPException) as exc_info:
        await auth_routes.password_login(body, FakeRequest(), return_to=None)
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_bcrypt4_era_hash_of_a_long_password_still_signs_in(redis, monkeypatch) -> None:
    _password_user(monkeypatch, _hash(LONG_PASSWORD.encode()[:72]))
    redis.store[KEY] = 2
    body = auth_routes.PasswordLoginRequest(login_id="admin", password=LONG_PASSWORD)

    response = await auth_routes.password_login(body, FakeRequest(), return_to=None)

    assert response.status_code == 200
    assert KEY not in redis.store  # failures cleared on success
