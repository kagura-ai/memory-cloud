"""The second factor draws on the account's sign-in budget.

``password_login`` counts wrong passwords per account (``_MAX_LOGIN_ATTEMPTS``
in ``_LOGIN_LOCKOUT_SECONDS``). These tests pin that the TOTP step of an
MFA-enabled account shares that budget:

- a wrong code counts; the budget spent, ``/mfa/verify`` answers 429 even on
  a pending token issued earlier. The attempt is taken with one ``INCR``
  before the code is checked, so requests racing at the limit on several
  workers do not each get a guess;
- a correct password does not clear the counter of an MFA-enabled account and
  issues no pending token while the account is locked;
- only the completed sign-in (second factor passed) clears it;
- an accepted code is not accepted a second time;
- the wrong code that spends the budget notifies the owner — a wrong password
  does not, since anyone who knows a login id can send one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pyotp
import pytest
from fastapi import HTTPException

from api.routes import auth as auth_routes
from auth.password import hash_password
from services.security_notification_service import SecurityEvent
from tests.redis_fake_ops import SessionFakeOps
from utils.exceptions import AuthenticationError, InvalidCredentialsError

PASSWORD = "Correct-Horse-1!"
SECRET = pyotp.random_base32()
USER_ID = "u-mfa"
COUNTER = f"login_attempts:user:{USER_ID}"
MAX = auth_routes._MAX_LOGIN_ATTEMPTS


class FakeRedis(SessionFakeOps):
    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str):
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    def setex(self, key: str, ttl: int, value) -> None:
        self.store[key] = value
        self.ttls[key] = ttl

    def incr(self, key: str) -> int:
        self.store[key] = int(self.store.get(key, 0)) + 1  # type: ignore[arg-type]
        return self.store[key]  # type: ignore[return-value]

    def pending_tokens(self) -> list[str]:
        return [k for k in self.store if k.startswith("mfa_pending:")]


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        cookies={}, headers={"user-agent": "pytest"}, client=SimpleNamespace(host="198.51.100.9")
    )


def _user(**kw) -> SimpleNamespace:
    base = {
        "user_id": USER_ID,
        "email": "person@example.test",
        "name": "Person",
        "role": "user",
        "password_hash": hash_password(PASSWORD),
        "totp_enabled": True,
        "totp_secret": "enc",
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    manager = MagicMock()
    manager._redis = FakeRedis()
    monkeypatch.setattr(auth_routes, "_session_manager", manager)
    return manager._redis


@pytest.fixture
def account(monkeypatch, redis) -> SimpleNamespace:
    """An MFA-enabled account; the TOTP secret is real, so are the codes."""
    user = _user()
    lookup = MagicMock()
    lookup.scalar_one_or_none.return_value = user
    db = MagicMock()
    db.execute = AsyncMock(return_value=lookup)

    async def _fake_db():
        yield db

    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    monkeypatch.setattr(auth_routes, "resolve_password_login_user", AsyncMock(return_value=user))
    monkeypatch.setattr(auth_routes, "get_encryptor", lambda: MagicMock(decrypt=lambda _: SECRET))
    monkeypatch.setattr(auth_routes, "_open_password_session", AsyncMock(return_value="sess-1"))
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    monkeypatch.setattr(auth_routes, "note_browser_sign_in", AsyncMock())
    notice = MagicMock()
    monkeypatch.setattr(auth_routes, "spawn_security_notification", notice)
    return SimpleNamespace(user=user, notice=notice)


async def _password_step(password: str = PASSWORD) -> str:
    body = auth_routes.PasswordLoginRequest(login_id="person@example.test", password=password)
    result = await auth_routes.password_login(body, _request(), return_to=None)
    assert result.mfa_required is True
    return result.mfa_session_token


async def _code_step(token: str, code: str):
    body = auth_routes.MfaVerifyRequest(mfa_session_token=token, totp_code=code)
    return await auth_routes.mfa_verify(body, _request(), return_to=None)


def _wrong_code() -> str:
    # Five digits never equal a six-digit code: wrong in every time step.
    return "12345"


@pytest.mark.asyncio
async def test_wrong_codes_spend_the_budget_and_then_answer_429(redis, account) -> None:
    tokens = [await _password_step() for _ in range(MAX + 1)]

    for token in tokens[:MAX]:
        with pytest.raises(AuthenticationError) as exc_info:
            await _code_step(token, _wrong_code())
        assert not isinstance(exc_info.value, InvalidCredentialsError)
        assert f"mfa_pending:{token}" not in redis.store
    assert redis.store[COUNTER] == MAX

    with pytest.raises(HTTPException) as exc_info:
        await _code_step(tokens[MAX], _wrong_code())
    assert exc_info.value.status_code == 429
    # The budget is spent at the password step too: no new pending token.
    with pytest.raises(HTTPException) as exc_info:
        await _password_step()
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_correct_password_does_not_reset_the_budget(redis, account) -> None:
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            await _code_step(await _password_step(), _wrong_code())
    assert redis.store[COUNTER] == 2

    await _password_step()

    assert redis.store[COUNTER] == 2


@pytest.mark.asyncio
async def test_locked_account_gets_no_pending_token_for_a_correct_password(redis, account) -> None:
    redis.store[COUNTER] = MAX

    with pytest.raises(HTTPException) as exc_info:
        await _password_step()

    assert exc_info.value.status_code == 429
    assert redis.pending_tokens() == []
    auth_routes._open_password_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_sign_in_clears_the_budget(redis, account) -> None:
    with pytest.raises(AuthenticationError):
        await _code_step(await _password_step(), _wrong_code())
    assert redis.store[COUNTER] == 1

    response = await _code_step(await _password_step(), pyotp.TOTP(SECRET).now())

    assert response.status_code == 200
    assert COUNTER not in redis.store
    account.notice.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_password_still_counts_for_an_mfa_account(redis, account) -> None:
    body = auth_routes.PasswordLoginRequest(login_id="person@example.test", password="wrong")
    with pytest.raises(InvalidCredentialsError):
        await auth_routes.password_login(body, _request(), return_to=None)
    assert redis.store[COUNTER] == 1

    # A wrong password and a wrong code draw on the same budget.
    with pytest.raises(AuthenticationError):
        await _code_step(await _password_step(), _wrong_code())
    assert redis.store[COUNTER] == 2


@pytest.mark.asyncio
async def test_an_accepted_code_is_not_accepted_twice(redis, account) -> None:
    code = pyotp.TOTP(SECRET).now()
    response = await _code_step(await _password_step(), code)
    assert response.status_code == 200

    token = await _password_step()
    with pytest.raises(AuthenticationError) as exc_info:
        await _code_step(token, code)

    assert "Invalid TOTP code" in str(exc_info.value.message)
    assert f"mfa_pending:{token}" not in redis.store
    assert redis.store[COUNTER] == 1
    # The marker outlives the code's validity window, no longer.
    used = [k for k in redis.store if k.startswith(f"mfa_totp_used:{USER_ID}:")]
    assert len(used) == 1 and code not in used[0]
    assert redis.ttls[used[0]] >= 90


@pytest.mark.asyncio
async def test_a_fresh_code_is_accepted_after_a_used_one(redis, account) -> None:
    totp = pyotp.TOTP(SECRET)
    await _code_step(await _password_step(), totp.now())

    # The next time step's code is a different code: accepted.
    next_code = totp.at(datetime.now(tz=UTC), counter_offset=1)
    response = await _code_step(await _password_step(), next_code)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_spending_the_budget_with_a_code_notifies_the_owner(redis, account) -> None:
    tokens = [await _password_step() for _ in range(MAX)]

    for token in tokens[:-1]:
        with pytest.raises(AuthenticationError):
            await _code_step(token, _wrong_code())
    account.notice.assert_not_called()

    with pytest.raises(AuthenticationError):
        await _code_step(tokens[-1], _wrong_code())

    account.notice.assert_called_once()
    kwargs = account.notice.call_args.kwargs
    assert kwargs["user_id"] == USER_ID
    assert kwargs["event"] == SecurityEvent.SECOND_FACTOR_LOCKED
    assert kwargs["request"].client.host == "198.51.100.9"


@pytest.mark.asyncio
async def test_concurrent_failures_at_the_limit_get_one_guess_and_one_notice(
    redis, account
) -> None:
    # Two pending tokens minted in advance, one attempt left in the budget,
    # both verify calls in flight at once (as on two uvicorn workers). The
    # attempt is reserved with one INCR before the code is checked: the first
    # call gets the last guess (401, and it is the one that notifies), the
    # second is refused (429) without a check and consumes nothing.
    first, second = await _password_step(), await _password_step()
    redis.store[COUNTER] = MAX - 1

    results = await asyncio.gather(
        _code_step(first, _wrong_code()), _code_step(second, _wrong_code()), return_exceptions=True
    )

    guessed, refused = results
    assert isinstance(guessed, AuthenticationError)
    assert isinstance(refused, HTTPException) and refused.status_code == 429
    assert f"mfa_pending:{first}" not in redis.store
    assert f"mfa_pending:{second}" in redis.store
    assert f"mfa_pending_cred:{second}" in redis.store
    assert redis.store[COUNTER] == MAX + 1
    account.notice.assert_called_once()


@pytest.mark.asyncio
async def test_a_refused_attempt_past_the_limit_never_notifies_again(redis, account) -> None:
    # Only the INCR that lands exactly on the limit notifies.
    tokens = [await _password_step() for _ in range(3)]
    redis.store[COUNTER] = MAX
    for token in tokens:
        with pytest.raises(HTTPException) as exc_info:
            await _code_step(token, _wrong_code())
        assert exc_info.value.status_code == 429
    account.notice.assert_not_called()
    assert redis.store[COUNTER] == MAX + 3


@pytest.mark.asyncio
async def test_spending_the_budget_with_passwords_does_not_notify(redis, account) -> None:
    body = auth_routes.PasswordLoginRequest(login_id="person@example.test", password="wrong")
    for _ in range(MAX):
        with pytest.raises(InvalidCredentialsError):
            await auth_routes.password_login(body, _request(), return_to=None)

    assert redis.store[COUNTER] == MAX
    account.notice.assert_not_called()
