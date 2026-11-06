"""Self-service MFA updates hold the account lock and require fresh credentials."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pyotp
import pytest
from fastapi import BackgroundTasks, HTTPException, Response
from redis.exceptions import ConnectionError as RedisConnectionError

from api.routes import auth
from api.routes import mfa_settings as routes
from auth.password import hash_password
from auth.recovery_codes import claim_login_code
from auth.session import SESSION_COOKIE_NAME
from models.auth import User
from services.security_notification_service import SecurityEvent
from tests.api.test_mfa_attempt_limit import FakeRedis
from utils.exceptions import AuthenticationError, ConflictError, CurrentPasswordMismatchError


@pytest.fixture
def env(monkeypatch, fernet_api_key_secret):
    user = User(user_id="mfa-enroll", email="mfa@example.test", password_hash=hash_password("pass"))
    db = MagicMock()
    db.scalar = AsyncMock(return_value=user)
    db.commit = AsyncMock()
    manager = MagicMock()
    manager._redis = FakeRedis()
    monkeypatch.setattr(auth, "_session_manager", manager)
    monkeypatch.setattr(routes, "_over_limit", AsyncMock(return_value=False))
    req = MagicMock(cookies={SESSION_COOKIE_NAME: "browser-1"}, headers={})
    notice = MagicMock()
    monkeypatch.setattr(routes, "schedule_security_notification", notice)
    return user, db, req, manager, notice


async def setup(env, password="pass"):
    user, db, req, *_ = env
    return await routes.setup_mfa(
        routes.SetupBody(current_password=password), req, Response(), {"user_id": user.user_id}, db
    )


async def enable(env, code):
    user, db, req, *_ = env
    return await routes.enable_mfa(
        routes.CodeBody(code=code),
        req,
        Response(),
        {"user_id": user.user_id},
        BackgroundTasks(),
        db,
    )


@pytest.mark.asyncio
async def test_enroll_consume_and_disable(env):
    user, db, req, manager, notice = env
    enrolled = await setup(env)
    assert enrolled.secret in enrolled.provisioning_uri
    (pending_key,) = manager._redis.store.keys()
    assert enrolled.secret not in manager._redis.get(pending_key)
    assert manager._redis.ttls[pending_key] == 300

    codes = (await enable(env, pyotp.TOTP(enrolled.secret).now())).recovery_codes
    assert len(set(codes)) == 8
    assert user.totp_enabled and user.totp_secret != enrolled.secret
    assert not set(codes).intersection(user.totp_recovery_hashes)
    assert pending_key not in manager._redis.store
    assert notice.call_args.kwargs["event"] == SecurityEvent.MFA_ENABLED
    assert db.add.call_args.args[0].action == "mfa_enabled"
    manager.delete_user_sessions.assert_called_once_with(
        user.user_id, exclude_session_id="browser-1", strict=True
    )
    epoch = user.mfa_generation
    assert await claim_login_code(db, user.user_id, codes[0].upper())
    assert not await claim_login_code(db, user.user_id, codes[0])
    response = await routes.disable_mfa(
        routes.DisableBody(current_password="pass", code=codes[1]),
        req,
        {"user_id": user.user_id},
        BackgroundTasks(),
        db,
    )
    assert response.status_code == 204
    assert not user.totp_enabled and user.totp_secret is None and user.totp_recovery_hashes is None
    assert epoch != user.mfa_generation
    assert notice.call_args.kwargs["event"] == SecurityEvent.MFA_DISABLED
    assert db.add.call_args.args[0].action == "mfa_disabled"


@pytest.mark.asyncio
async def test_enrollment_requires_password_session_and_unchanged_credentials(env):
    user, _, req, *_ = env
    with pytest.raises(CurrentPasswordMismatchError):
        await setup(env, "wrong")
    enrollment = await setup(env)
    code = pyotp.TOTP(enrollment.secret).now()
    req.cookies[SESSION_COOKIE_NAME] = "second-browser"
    with pytest.raises(ConflictError):
        await enable(env, code)
    req.cookies[SESSION_COOKIE_NAME] = "browser-1"
    user.password_hash = hash_password("reset")
    with pytest.raises(ConflictError):
        await enable(env, code)
    assert not user.totp_enabled


@pytest.mark.asyncio
async def test_budget_cleanup_failure_still_delivers_recovery_codes_and_notice(env, monkeypatch):
    enrollment = await setup(env)
    monkeypatch.setattr(
        auth, "_clear_login_failures", MagicMock(side_effect=RedisConnectionError())
    )
    response = await enable(env, pyotp.TOTP(enrollment.secret).now())
    assert len(response.recovery_codes) == 8
    assert env[-1].call_args.kwargs["event"] == SecurityEvent.MFA_ENABLED


@pytest.mark.asyncio
async def test_regenerate_requires_fresh_factor_even_if_account_has_no_codes(env, monkeypatch):
    fixed_time = 1_700_000_010

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(fixed_time, tz)

    # Exercise real PyOTP and replay checks across every awaited password check
    # without a wall-clock boundary moving the earlier code outside the window.
    monkeypatch.setattr(pyotp.totp, "datetime", SimpleNamespace(datetime=FixedDatetime))
    user, db, req, _, notice = env
    enrollment = await setup(env)
    totp = pyotp.TOTP(enrollment.secret)
    current = totp.now()
    enabled = await enable(env, current)

    async def regenerate(code):
        return await routes.regenerate_recovery_codes(
            routes.DisableBody(current_password="pass", code=code),
            req,
            Response(),
            {"user_id": user.user_id},
            BackgroundTasks(),
            db,
        )

    for invalid in [current, "invalid"]:
        with pytest.raises(AuthenticationError):
            await regenerate(invalid)
    replacement = await regenerate(enabled.recovery_codes[0])
    with pytest.raises(AuthenticationError):
        await regenerate(enabled.recovery_codes[0])
    assert len(replacement.recovery_codes) == 8
    # An admin whose factor predates recovery codes can generate a first set.
    user.totp_recovery_hashes = None
    response = await regenerate(totp.at(fixed_time - 30))
    assert len(response.recovery_codes) == 8
    assert not await claim_login_code(db, user.user_id, enabled.recovery_codes[0])
    assert await claim_login_code(db, user.user_id, response.recovery_codes[0])
    assert notice.call_args.kwargs["event"] == SecurityEvent.RECOVERY_CODES_REPLACED


@pytest.mark.asyncio
async def test_unicode_equivalent_cannot_replay_enrollment_code(env):
    enrollment = await setup(env)
    code = pyotp.TOTP(enrollment.secret).now()
    await enable(env, code)
    alternate = "".join(chr(ord(digit) + 0xFEE0) for digit in code)
    user, db, req, *_ = env
    with pytest.raises(AuthenticationError):
        await routes.disable_mfa(
            routes.DisableBody(current_password="pass", code=alternate),
            req,
            {"user_id": user.user_id},
            BackgroundTasks(),
            db,
        )
    assert user.totp_enabled


@pytest.mark.asyncio
async def test_wrong_enrollment_codes_spend_login_budget(env):
    await setup(env)
    for _ in range(auth._MAX_LOGIN_ATTEMPTS):
        with pytest.raises(AuthenticationError):
            await enable(env, "wrong")
    with pytest.raises(HTTPException) as exc:
        await enable(env, "wrong")
    assert exc.value.status_code == 429
