"""A sign-in that may connect to an email sign-up account after an email confirmation (#2786).

``ensure_user``'s new-user path (#1734) links a Google identity to the account
created by email sign-up for the same address only when Google is
authoritative for the mailbox. A GitHub sign-in, or a Google one without that
authority, ends in ``ProviderLinkConfirmable``: a ``ConflictError`` subclass
(so any caller that does not handle it still answers ``email_in_use``) raised
only after every other refusal passed. It carries the account id and an
account stamp for the confirmation, never in its message or details. Mocked
DB; the Postgres behaviour is in ``tests/integration/test_provider_link_confirm_db.py``.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from auth.roles import RoleManager, account_stamp, email_account_link_refusal
from config.settings import get_settings
from tests.auth.test_role_manager_auto_link import (
    _db,
    _email_account,
    _patch_db,
    _result,
    _sequence,
    _sign_in,
)
from utils.exceptions import ConflictError, ProviderLinkConfirmable


@pytest.mark.parametrize(
    ("provider", "email"),
    [
        ("github", "person@gmail.com"),
        ("google", "person@gmail.com"),
        ("github", "Person@Gmail.com"),
    ],
)
@pytest.mark.asyncio
async def test_non_authoritative_sign_in_is_confirmable(provider, email) -> None:
    account = _email_account()
    db = _db(*_sequence([account], already_linked=None), _result(first=None))
    with pytest.raises(ProviderLinkConfirmable) as caught:
        await _sign_in(db, provider=provider, email=email, authoritative=False)

    exc = caught.value
    assert isinstance(exc, ConflictError)
    assert exc.account_user_id == "email:abc"
    assert exc.account_stamp == account_stamp(account)
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_exception_serializes_like_a_plain_conflict() -> None:
    """Nothing about the account reaches the message or the details."""
    account = _email_account()
    db = _db(*_sequence([account], already_linked=None), _result(first=None))
    with pytest.raises(ProviderLinkConfirmable) as caught:
        await _sign_in(db, provider="github", authoritative=False)

    plain = ConflictError("Email address is already in use by another account")
    exc = caught.value
    assert (exc.message, exc.status_code, exc.error_code, exc.details) == (
        plain.message,
        plain.status_code,
        plain.error_code,
        plain.details,
    )
    assert "email:abc" not in str(exc)
    assert exc.account_stamp not in str(exc)


@pytest.mark.parametrize(
    ("overrides", "already_linked"),
    [
        ({"totp_enabled": True}, None),
        ({"role": "admin"}, None),
        ({"role": "read_only"}, None),
        ({"provider_auto_link_blocked_at": datetime(2026, 2, 1)}, None),
        ({"login_id": "admin"}, None),
        ({"auth_method": "oauth"}, None),
        ({"email_verified_at": None}, None),
        ({"email": "Person@gmail.com"}, None),
        ({}, (1,)),
    ],
)
@pytest.mark.asyncio
async def test_every_other_refusal_stays_a_plain_conflict(overrides, already_linked) -> None:
    seq = _sequence([_email_account(**overrides)], already_linked=already_linked)
    db = _db(*seq)
    with pytest.raises(ConflictError) as caught:
        await _sign_in(db, provider="github", authoritative=False)
    assert type(caught.value) is ConflictError
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_an_unverified_google_address_is_a_plain_conflict() -> None:
    db = _db(*_sequence([_email_account()], already_linked=None))
    with _patch_db(db), pytest.raises(ConflictError) as caught:
        await RoleManager(use_postgres=True).ensure_user(
            email="person@gmail.com",
            user_id="google-sub-1",
            auth_provider="google",
            email_verified=False,
            email_authoritative=False,
        )
    assert type(caught.value) is ConflictError


@pytest.mark.asyncio
async def test_a_non_ascii_address_is_a_plain_conflict() -> None:
    db = _db(*_sequence([_email_account(email="person@gmail.com")]))
    with pytest.raises(ConflictError) as caught:
        await _sign_in(db, provider="github", email="person@gmaıl.com", authoritative=False)
    assert type(caught.value) is ConflictError


@pytest.mark.asyncio
async def test_switch_off_keeps_the_plain_conflict(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "enable_provider_link_confirmation", False)
    db = _db(*_sequence([_email_account()], already_linked=None), _result(first=None))
    with pytest.raises(ConflictError) as caught:
        await _sign_in(db, provider="github", authoritative=False)
    assert type(caught.value) is ConflictError


@pytest.mark.asyncio
async def test_several_holders_stay_a_plain_conflict() -> None:
    db = _db(*_sequence([_email_account(), _email_account(id=8, user_id="email:def")]))
    with pytest.raises(ConflictError) as caught:
        await _sign_in(db, provider="github", authoritative=False)
    assert type(caught.value) is ConflictError


class TestRowHelpers:
    def test_eligible_account_has_no_refusal(self) -> None:
        assert email_account_link_refusal(_email_account(), address="person@gmail.com") is None

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"login_id": "x"}, "not_an_email_signup_account"),
            ({"auth_method": "oauth"}, "not_an_email_signup_account"),
            ({"email_verified_at": None}, "not_an_email_signup_account"),
            ({"email": "other@gmail.com"}, "not_an_email_signup_account"),
            ({"totp_enabled": True}, "totp_enabled"),
            ({"role": "admin"}, "role"),
            ({"provider_auto_link_blocked_at": datetime(2026, 2, 1)}, "blocked_by_owner"),
        ],
    )
    def test_refusal_reasons(self, overrides, reason) -> None:
        refusal = email_account_link_refusal(
            _email_account(**overrides), address="person@gmail.com"
        )
        assert refusal == reason

    def test_the_address_is_compared_normalized(self) -> None:
        assert email_account_link_refusal(_email_account(), address=" Person@Gmail.COM ") is None

    @pytest.mark.parametrize(
        "change",
        [
            {"password_hash": "other"},
            {"mfa_generation": "g2"},
            {"totp_enabled": True},
            {"role": "admin"},
            {"provider_auto_link_blocked_at": datetime(2026, 3, 1)},
            {"email": "new@gmail.com"},
            {"id": 99},
            {"user_id": "email:other"},
        ],
    )
    def test_the_stamp_changes_with_the_security_state(self, change) -> None:
        assert account_stamp(_email_account()) != account_stamp(_email_account(**change))

    def test_the_stamp_ignores_a_sign_in(self) -> None:
        before = _email_account()
        after = _email_account(name="Renamed")
        after.last_login_at = datetime(2026, 5, 1)
        assert account_stamp(before) == account_stamp(after)
