"""Every browser sign-in path records the device (#1769).

The password and MFA routes are driven directly; the two OAuth callbacks are
checked by inspecting the handler source, because driving them needs the
whole provider exchange. All four must call ``note_browser_sign_in`` after
the session cookie is set, and the MFA path only once the second factor
passed.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes import auth as auth_routes
from auth.password import hash_password
from tests.api.test_password_login_email import FakeRedis

PASSWORD = "Correct-Horse-1!"


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        cookies={}, headers={"user-agent": "pytest"}, client=SimpleNamespace(host="203.0.113.9")
    )


def _user(**kw) -> SimpleNamespace:
    base = {
        "user_id": "u-device",
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
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    manager = MagicMock()
    manager._redis = FakeRedis()
    monkeypatch.setattr(auth_routes, "_session_manager", manager)

    async def _fake_db():
        yield MagicMock()

    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    monkeypatch.setattr(
        auth_routes, "_create_session_and_workspace", AsyncMock(return_value="sess-1")
    )
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    resolver = AsyncMock(return_value=_user())
    monkeypatch.setattr(auth_routes, "resolve_password_login_user", resolver)
    note = AsyncMock()
    monkeypatch.setattr(auth_routes, "note_browser_sign_in", note)
    return {"redis": manager._redis, "resolver": resolver, "note": note}


@pytest.mark.asyncio
async def test_password_login_records_the_device_after_the_cookie(harness) -> None:
    body = auth_routes.PasswordLoginRequest(login_id="person@example.test", password=PASSWORD)

    response = await auth_routes.password_login(body, _request(), return_to=None)

    harness["note"].assert_awaited_once()
    args, kwargs = harness["note"].await_args
    assert args[1] is response  # the response that carries the session cookie
    assert kwargs == {"user_id": "u-device", "sign_in_method": "Password"}
    assert "kagura_session=sess-1" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_mfa_login_records_the_device_only_after_the_second_factor(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness["resolver"].return_value = _user(totp_enabled=True, totp_secret="enc")
    body = auth_routes.PasswordLoginRequest(login_id="person@example.test", password=PASSWORD)

    pending = await auth_routes.password_login(body, _request(), return_to=None)

    assert pending.mfa_required is True
    harness["note"].assert_not_awaited()

    monkeypatch.setattr(auth_routes, "get_encryptor", lambda: MagicMock(decrypt=lambda _: "s"))
    monkeypatch.setattr(auth_routes, "verify_totp", lambda *_: True)
    user_lookup = MagicMock()
    user_lookup.scalar_one_or_none.return_value = _user(totp_enabled=True, totp_secret="enc")
    db = MagicMock()
    db.execute = AsyncMock(return_value=user_lookup)

    async def _db_with_user():
        yield db

    monkeypatch.setattr(auth_routes, "get_db", _db_with_user)

    response = await auth_routes.mfa_verify(
        auth_routes.MfaVerifyRequest(mfa_session_token=pending.mfa_session_token, totp_code="1"),
        _request(),
        return_to=None,
    )

    harness["note"].assert_awaited_once()
    args, kwargs = harness["note"].await_args
    assert args[1] is response
    assert kwargs == {"user_id": "u-device", "sign_in_method": "Password"}


@pytest.mark.parametrize(
    ("handler", "method"),
    [(auth_routes.google_callback, "Google"), (auth_routes.github_callback, "GitHub")],
)
def test_oauth_callbacks_record_the_device_after_the_cookie(handler, method: str) -> None:
    source = inspect.getsource(handler)
    cookie_at = source.index("_set_session_cookie(redirect, session_id)")
    note_at = source.index("await note_browser_sign_in(")
    assert cookie_at < note_at
    assert f'sign_in_method="{method}"' in source[note_at:]
