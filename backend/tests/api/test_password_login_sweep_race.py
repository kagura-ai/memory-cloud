"""Password sign-in vs. a password write's session sweep (#1809).

The race itself, against real Postgres row locks, is in
``tests/services/test_password_account_service.py::TestPasswordLoginRacingTheSweep``.
These tests pin what the routes do with the answer: the sign-in re-checks the
password AFTER its session exists, and when the password it verified is no
longer the committed one, deletes that session and answers 401 — for a plain
sign-in and for one finished with a second factor.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.routes import auth as auth_routes
from auth.password import hash_password
from services.password_account_service import credential_fingerprint
from tests.redis_fake_ops import SessionFakeOps
from utils.exceptions import AuthenticationError, InvalidCredentialsError

PASSWORD = "Correct-Horse-1!"


class FakeRedis(SessionFakeOps):
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def get(self, key: str):
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    def setex(self, key: str, _ttl: int, value) -> None:
        self.store[key] = value

    def incr(self, key: str) -> int:
        self.store[key] = int(self.store.get(key, 0)) + 1  # type: ignore[arg-type]
        return self.store[key]  # type: ignore[return-value]


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        cookies={}, headers={"user-agent": "pytest"}, client=SimpleNamespace(host="198.51.100.9")
    )


def _user(**kw) -> SimpleNamespace:
    base = {
        "user_id": "u-1",
        "email": "person@example.test",
        "name": "Person",
        "role": "user",
        "password_hash": hash_password(PASSWORD),
        "totp_enabled": False,
        "totp_secret": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def manager(monkeypatch) -> MagicMock:
    m = MagicMock()
    m._redis = FakeRedis()
    monkeypatch.setattr(auth_routes, "_session_manager", m)
    return m


@pytest.fixture
def wired(monkeypatch, manager) -> SimpleNamespace:
    """Everything around the re-check is stubbed; the re-check answer is set per test."""
    order: list[str] = []
    db = MagicMock()
    db.rollback = AsyncMock()

    async def _fake_db():
        yield db

    async def _create(**_kw):
        order.append("session")
        return "sess-1"

    recheck = AsyncMock(return_value=True)

    async def _recheck(*args):
        order.append("recheck")
        return await recheck(*args)

    user = _user()
    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    monkeypatch.setattr(auth_routes, "_create_session_and_workspace", _create)
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    monkeypatch.setattr(auth_routes, "note_browser_sign_in", AsyncMock())
    monkeypatch.setattr(auth_routes, "resolve_password_login_user", AsyncMock(return_value=user))
    monkeypatch.setattr(auth_routes, "password_unchanged", _recheck)
    return SimpleNamespace(user=user, db=db, order=order, recheck=recheck)


def _login_body() -> auth_routes.PasswordLoginRequest:
    return auth_routes.PasswordLoginRequest(login_id="person@example.test", password=PASSWORD)


class TestPasswordLogin:
    @pytest.mark.asyncio
    async def test_rechecks_the_verified_password_after_the_session_exists(
        self, wired, manager
    ) -> None:
        response = await auth_routes.password_login(_login_body(), _request(), return_to=None)

        assert response.status_code == 200
        assert wired.order == ["session", "recheck"]
        _db, user_id, fingerprint = wired.recheck.await_args.args
        assert user_id == "u-1"
        assert fingerprint == credential_fingerprint(wired.user.password_hash)
        # The share lock is dropped as soon as the answer is in.
        wired.db.rollback.assert_awaited()
        manager.delete_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_password_changed_meanwhile_deletes_the_new_session(
        self, wired, manager
    ) -> None:
        wired.recheck.return_value = False

        with pytest.raises(InvalidCredentialsError):
            await auth_routes.password_login(_login_body(), _request(), return_to=None)

        manager.delete_session.assert_called_once_with("sess-1")

    @pytest.mark.asyncio
    async def test_a_failed_recheck_fails_closed_as_an_outage(self, wired, manager) -> None:
        # The session is not kept, but a correct password is not reported as
        # a wrong one: the database failed, so the answer is "try again".
        wired.recheck.side_effect = RuntimeError("db down")

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.password_login(_login_body(), _request(), return_to=None)

        assert exc_info.value.status_code == 503
        manager.delete_session.assert_called_once_with("sess-1")


class TestMfaVerify:
    @pytest.fixture
    def mfa(self, wired, manager, monkeypatch) -> SimpleNamespace:
        wired.user.totp_enabled = True
        wired.user.totp_secret = "enc"
        result = MagicMock()
        result.scalar_one_or_none.return_value = wired.user
        wired.db.execute = AsyncMock(return_value=result)
        monkeypatch.setattr(
            auth_routes, "get_encryptor", lambda: SimpleNamespace(decrypt=lambda _s: "secret")
        )
        monkeypatch.setattr(auth_routes, "verify_totp", lambda _secret, code: code == "123456")
        return wired

    async def _password_step(self) -> str:
        result = await auth_routes.password_login(_login_body(), _request(), return_to=None)
        assert result.mfa_required is True
        return result.mfa_session_token

    def _verify_body(self, token: str) -> auth_routes.MfaVerifyRequest:
        return auth_routes.MfaVerifyRequest(mfa_session_token=token, totp_code="123456")

    @pytest.mark.asyncio
    async def test_the_password_step_keeps_the_fingerprint_beside_the_pending_step(
        self, mfa, manager
    ) -> None:
        token = await self._password_step()
        stored = manager._redis.store[f"mfa_pending_cred:{token}"]
        assert stored == credential_fingerprint(mfa.user.password_hash)
        assert mfa.user.password_hash not in stored

    @pytest.mark.asyncio
    async def test_a_password_changed_after_the_password_step_deletes_the_session(
        self, mfa, manager
    ) -> None:
        token = await self._password_step()
        mfa.recheck.return_value = False

        with pytest.raises(InvalidCredentialsError):
            await auth_routes.mfa_verify(self._verify_body(token), _request(), return_to=None)

        assert mfa.order == ["session", "recheck"]
        _db, _uid, fingerprint = mfa.recheck.await_args.args
        assert fingerprint == credential_fingerprint(mfa.user.password_hash)
        manager.delete_session.assert_called_once_with("sess-1")
        # Single use, like the pending token.
        assert f"mfa_pending_cred:{token}" not in manager._redis.store

    @pytest.mark.asyncio
    async def test_an_unchanged_password_signs_in(self, mfa, manager) -> None:
        token = await self._password_step()
        response = await auth_routes.mfa_verify(
            self._verify_body(token), _request(), return_to=None
        )
        assert response.status_code == 200
        manager.delete_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_pending_step_without_a_fingerprint_signs_nobody_in(self, mfa, manager) -> None:
        # A pending step written before #1809 (5-minute lifetime) carries no
        # fingerprint: the user signs in again rather than skipping the check.
        manager._redis.setex("mfa_pending:old", 300, "u-1")

        with pytest.raises(AuthenticationError):
            await auth_routes.mfa_verify(self._verify_body("old"), _request(), return_to=None)

        assert mfa.order == []
        assert "mfa_pending:old" not in manager._redis.store

    @pytest.mark.asyncio
    async def test_one_pending_step_signs_in_once(self, mfa, manager) -> None:
        # Two requests that both read the step before either deleted it: only
        # the one whose DELETE removed the fingerprint goes on.
        token = await self._password_step()
        real_delete = manager._redis.delete

        def _lost_race(*keys: str) -> int:
            if any(k.startswith("mfa_pending_cred:") for k in keys):
                real_delete(*keys)
                return 0
            return real_delete(*keys)

        manager._redis.delete = _lost_race

        with pytest.raises(AuthenticationError):
            await auth_routes.mfa_verify(self._verify_body(token), _request(), return_to=None)

        assert mfa.order == []

    @pytest.mark.asyncio
    async def test_the_losing_request_consumes_nothing(self, mfa, manager, monkeypatch) -> None:
        # The fingerprint is taken before the TOTP check and the terms key, so
        # a request that loses the race neither checks a code nor takes the
        # winner's terms acceptance.
        token = await self._password_step()
        manager._redis.setex(f"mfa_pending_terms:{token}", 300, "2026-01-01")
        manager._redis.delete(f"mfa_pending_cred:{token}")
        checked: list[str] = []
        monkeypatch.setattr(
            auth_routes, "verify_totp", lambda _secret, code: checked.append(code) or True
        )

        with pytest.raises(AuthenticationError):
            await auth_routes.mfa_verify(self._verify_body(token), _request(), return_to=None)

        assert checked == []
        assert f"mfa_pending_terms:{token}" in manager._redis.store
