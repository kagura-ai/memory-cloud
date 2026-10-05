"""Password sign-in vs. a password write's session sweep (#1809).

The race itself, against real Postgres row locks, is in
``tests/services/test_password_account_service.py::TestPasswordLoginRacingTheSweep``.
These tests pin what the routes do with the answer: the sign-in re-checks the
password AFTER its session exists, and when the password it verified is no
longer the committed one, deletes that session and answers 401 — for a plain
sign-in and for one finished with a second factor.

A refused sign-in has no other effect (#1878): the account's other sessions,
its personal workspace and ``last_login_at`` are touched only once the
re-check has accepted the sign-in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.routes import auth as auth_routes
from auth.password import hash_password
from auth.session import SessionManager
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

    complete = AsyncMock(return_value=True)

    async def _complete(*args):
        order.append("complete")
        return await complete(*args)

    recheck = AsyncMock(return_value=True)

    async def _recheck(*args):
        order.append("recheck")
        return await recheck(*args)

    user = _user()
    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    monkeypatch.setattr(auth_routes, "_create_password_session", _create)
    monkeypatch.setattr(auth_routes, "_complete_password_sign_in", _complete)
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    monkeypatch.setattr(auth_routes, "note_browser_sign_in", AsyncMock())
    monkeypatch.setattr(auth_routes, "resolve_password_login_user", AsyncMock(return_value=user))
    monkeypatch.setattr(auth_routes, "password_unchanged", _recheck)
    return SimpleNamespace(user=user, db=db, order=order, recheck=recheck, complete=complete)


def _login_body() -> auth_routes.PasswordLoginRequest:
    return auth_routes.PasswordLoginRequest(login_id="person@example.test", password=PASSWORD)


class TestPasswordLogin:
    @pytest.mark.asyncio
    async def test_rechecks_the_verified_password_after_the_session_exists(
        self, wired, manager
    ) -> None:
        response = await auth_routes.password_login(_login_body(), _request(), return_to=None)

        assert response.status_code == 200
        # #1878: the other sessions, the workspace and last_login_at come last.
        assert wired.order == ["session", "recheck", "complete"]
        wired.complete.assert_awaited_once_with("u-1", "person@example.test", "sess-1")
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
        assert wired.order == ["session", "recheck"]
        manager.delete_user_sessions.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_recheck_fails_closed_as_an_outage(self, wired, manager) -> None:
        # The session is not kept, but a correct password is not reported as
        # a wrong one: the database failed, so the answer is "try again".
        wired.recheck.side_effect = RuntimeError("db down")

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.password_login(_login_body(), _request(), return_to=None)

        assert exc_info.value.status_code == 503
        manager.delete_session.assert_called_once_with("sess-1")
        assert wired.order == ["session", "recheck"]
        manager.delete_user_sessions.assert_not_called()


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
        manager.delete_user_sessions.assert_not_called()
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
        assert mfa.order == ["session", "recheck", "complete"]

    @pytest.mark.asyncio
    async def test_a_wrong_code_deletes_only_the_pending_token(self, mfa, manager) -> None:
        # The single-use guard already took the fingerprint key; the wrong-code
        # branch has only the pending token left to delete (#1878).
        token = await self._password_step()
        deleted: list[tuple[str, ...]] = []
        real_delete = manager._redis.delete

        def _spy(*keys: str) -> int:
            deleted.append(keys)
            return real_delete(*keys)

        manager._redis.delete = _spy
        body = auth_routes.MfaVerifyRequest(mfa_session_token=token, totp_code="000000")

        with pytest.raises(AuthenticationError):
            await auth_routes.mfa_verify(body, _request(), return_to=None)

        assert deleted == [(f"mfa_pending_cred:{token}",), (f"mfa_pending:{token}",)]
        assert f"mfa_pending:{token}" not in manager._redis.store
        assert mfa.order == []

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


class StoreRedis(FakeRedis):
    """``FakeRedis`` plus the calls a real ``SessionManager`` makes."""

    def ttl(self, _key: str) -> int:
        return 100


class TestARefusedSignInHasNoSideEffects:
    """#1878, against a real SessionManager: what a refused sign-in leaves behind.

    A password change, set-up or removal keeps the session it was made from.
    A sign-in that verified the OLD password and is refused by the re-check
    must leave that session — and ``last_login_at`` — alone.
    """

    @pytest.fixture
    def real(self, monkeypatch) -> SimpleNamespace:
        fake = StoreRedis()
        monkeypatch.setattr(
            SessionManager, "_get_or_create_redis_client", staticmethod(lambda _url: fake)
        )
        manager = SessionManager(redis_url="redis://fake:6379")
        monkeypatch.setattr(auth_routes, "_session_manager", manager)

        user = _user(last_login_at=None)
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        async def _fake_db():
            yield db

        workspaces = MagicMock()
        workspaces.return_value.ensure_personal_workspace = AsyncMock()
        recheck = AsyncMock(return_value=True)
        monkeypatch.setattr(auth_routes, "get_db", _fake_db)
        monkeypatch.setattr(auth_routes, "WorkspaceService", workspaces)
        monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
        monkeypatch.setattr(auth_routes, "note_browser_sign_in", AsyncMock())
        monkeypatch.setattr(
            auth_routes, "resolve_password_login_user", AsyncMock(return_value=user)
        )
        monkeypatch.setattr(auth_routes, "password_unchanged", recheck)
        monkeypatch.setattr(
            auth_routes, "get_encryptor", lambda: SimpleNamespace(decrypt=lambda _s: "secret")
        )
        monkeypatch.setattr(auth_routes, "verify_totp", lambda _secret, code: code == "123456")

        kept = manager.create_session(
            {"sub": "u-1", "user_id": "u-1", "email": user.email, "name": "Person", "role": "user"}
        )
        return SimpleNamespace(
            manager=manager,
            user=user,
            db=db,
            recheck=recheck,
            kept=kept,
            workspace=workspaces.return_value.ensure_personal_workspace,
        )

    def _sessions(self, real: SimpleNamespace) -> set[str]:
        prefix = "session:"
        return {k[len(prefix) :] for k in real.manager._redis.store if k.startswith(prefix)}

    def _assert_untouched(self, real: SimpleNamespace) -> None:
        # Only the session that was there before; the new one is gone.
        assert self._sessions(real) == {real.kept}
        assert real.manager.get_session(real.kept) is not None
        assert real.user.last_login_at is None
        real.db.commit.assert_not_awaited()
        real.workspace.assert_not_awaited()

    async def _mfa_token(self, real: SimpleNamespace) -> str:
        real.user.totp_enabled = True
        real.user.totp_secret = "enc"
        result = await auth_routes.password_login(_login_body(), _request(), return_to=None)
        return result.mfa_session_token

    @pytest.mark.asyncio
    async def test_a_superseded_sign_in_keeps_the_other_session(self, real) -> None:
        real.recheck.return_value = False

        with pytest.raises(InvalidCredentialsError):
            await auth_routes.password_login(_login_body(), _request(), return_to=None)

        self._assert_untouched(real)

    @pytest.mark.asyncio
    async def test_a_failed_recheck_keeps_the_other_session(self, real) -> None:
        real.recheck.side_effect = RuntimeError("db down")

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.password_login(_login_body(), _request(), return_to=None)

        assert exc_info.value.status_code == 503
        self._assert_untouched(real)

    @pytest.mark.asyncio
    async def test_a_superseded_second_factor_keeps_the_other_session(self, real) -> None:
        token = await self._mfa_token(real)
        real.recheck.return_value = False
        body = auth_routes.MfaVerifyRequest(mfa_session_token=token, totp_code="123456")

        with pytest.raises(InvalidCredentialsError):
            await auth_routes.mfa_verify(body, _request(), return_to=None)

        self._assert_untouched(real)

    @pytest.mark.asyncio
    async def test_a_failed_recheck_at_the_second_factor_keeps_the_other_session(
        self, real
    ) -> None:
        token = await self._mfa_token(real)
        real.recheck.side_effect = RuntimeError("db down")
        body = auth_routes.MfaVerifyRequest(mfa_session_token=token, totp_code="123456")

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.mfa_verify(body, _request(), return_to=None)

        assert exc_info.value.status_code == 503
        self._assert_untouched(real)

    @pytest.mark.asyncio
    async def test_an_accepted_sign_in_replaces_the_other_sessions(self, real) -> None:
        response = await auth_routes.password_login(_login_body(), _request(), return_to=None)

        assert response.status_code == 200
        sessions = self._sessions(real)
        assert len(sessions) == 1 and real.kept not in sessions
        (new_session,) = sessions
        assert f"kagura_session={new_session}" in response.headers["set-cookie"]
        assert real.manager.get_session(new_session) is not None
        assert real.user.last_login_at is not None
        real.db.commit.assert_awaited_once()
        real.workspace.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_accepted_second_factor_replaces_the_other_sessions(self, real) -> None:
        token = await self._mfa_token(real)
        body = auth_routes.MfaVerifyRequest(mfa_session_token=token, totp_code="123456")

        response = await auth_routes.mfa_verify(body, _request(), return_to=None)

        assert response.status_code == 200
        sessions = self._sessions(real)
        assert len(sessions) == 1 and real.kept not in sessions
        assert real.user.last_login_at is not None

    @pytest.mark.asyncio
    async def test_overlapping_sign_ins_leave_exactly_one_live_session(self, real) -> None:
        # Both sign-ins write their session before either re-check answers.
        # Without the sweep lock each would then delete the other's session
        # and both would answer 200 with a dead cookie.
        release = asyncio.Event()
        waiting = 0

        async def _held_recheck(*_args) -> bool:
            nonlocal waiting
            waiting += 1
            await release.wait()
            return True

        real.recheck.side_effect = _held_recheck
        tasks = [
            asyncio.create_task(
                auth_routes.password_login(_login_body(), _request(), return_to=None)
            )
            for _ in range(2)
        ]
        while waiting < 2:
            await asyncio.sleep(0.01)
        assert len(self._sessions(real)) == 3  # the old one and two candidates
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        winners = [r for r in results if not isinstance(r, BaseException)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == 1
        assert isinstance(losers[0], HTTPException) and losers[0].status_code == 503
        (survivor,) = self._sessions(real)
        assert f"kagura_session={survivor}" in winners[0].headers["set-cookie"]
        assert real.manager.get_session(survivor) is not None
        # The lock is released.
        assert "signin_sweep_lock:u-1" not in real.manager._redis.store

    @pytest.mark.asyncio
    async def test_a_session_swept_after_the_recheck_signs_nobody_in(self, real) -> None:
        # A password write that starts after the re-check sweeps the new
        # session and keeps its own: the sign-in must not then sweep that one.
        async def _recheck_then_password_write(*_args) -> bool:
            real.manager.delete_user_sessions("u-1", exclude_session_id=real.kept)
            return True

        real.recheck.side_effect = _recheck_then_password_write

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.password_login(_login_body(), _request(), return_to=None)

        assert exc_info.value.status_code == 503
        self._assert_untouched(real)
        assert "signin_sweep_lock:u-1" not in real.manager._redis.store

    @pytest.mark.asyncio
    async def test_a_sweep_lock_that_stays_held_fails_closed(self, real, monkeypatch) -> None:
        monkeypatch.setattr(auth_routes, "_SIGN_IN_SWEEP_LOCK_WAIT_SECONDS", 0.1)
        real.manager._redis.set("signin_sweep_lock:u-1", "someone-else", nx=True, ex=10)

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.password_login(_login_body(), _request(), return_to=None)

        assert exc_info.value.status_code == 503
        self._assert_untouched(real)
        # Another holder's lock is not released by the loser.
        assert real.manager._redis.store["signin_sweep_lock:u-1"] == "someone-else"
