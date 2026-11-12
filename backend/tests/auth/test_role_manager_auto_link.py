"""A Google sign-in with the address of an email sign-up account (#1734), mocked DB.

``ensure_user``'s new-user path locks the address, reads every account that
holds it (any case), and either links this identity to the account email
sign-up created, or refuses with ``ConflictError`` (``email_in_use``). It never
inserts a second account for the address. The Postgres behaviour (row locks,
case variants, concurrency) is in ``tests/integration/test_email_signup_db.py``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from auth.roles import Role, RoleManager
from models.auth import AuditLog, UserOAuthProvider
from utils.exceptions import ConflictError


def _result(*, holders=None, first=None, one=None):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(holders or [])
    r.first.return_value = first
    r.scalar.return_value = 1
    r.scalar_one_or_none.return_value = one
    return r


def _email_account(**overrides):
    values = {
        "id": 7,
        "user_id": "email:abc",
        "email": "person@gmail.com",
        "name": "Person",
        "role": "user",
        "auth_method": "password",
        "login_id": None,
        "email_verified_at": datetime(2026, 1, 1),
        "totp_enabled": False,
        "provider_auto_link_blocked_at": None,
        "auth_provider": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    return db


def _patch_db(db):
    async def _fake_get_db():
        yield db

    return patch("db.base.get_db", new=_fake_get_db)


async def _sign_in(db, *, email="person@gmail.com", provider="google", authoritative=True):
    with (
        _patch_db(db),
        patch("services.security_notification_service.spawn_security_notification") as notify,
    ):
        role = await RoleManager(use_postgres=True).ensure_user(
            email=email,
            user_id="google-sub-1",
            name="Google Name",
            auth_provider=provider,
            email_verified=True,
            email_authoritative=authoritative,
        )
    return role, notify


def _sequence(holders, *, already_linked=None):
    # link lookup miss -> count -> advisory lock -> holders -> (provider check)
    seq = [_result(one=None), _result(), _result(), _result(holders=holders)]
    if already_linked is not None:
        seq.append(_result(first=already_linked))
    return seq


@pytest.mark.asyncio
async def test_authoritative_google_links_to_the_email_account() -> None:
    account = _email_account()
    db = _db(*_sequence([account], already_linked=None), _result(first=None))
    role, notify = await _sign_in(db)

    assert role == Role.USER
    added = [c.args[0] for c in db.add.call_args_list]
    link = next(o for o in added if isinstance(o, UserOAuthProvider))
    assert (link.user_id, link.provider, link.oauth_sub) == ("email:abc", "google", "google-sub-1")
    audit = next(o for o in added if isinstance(o, AuditLog))
    assert audit.action == "oauth_provider_auto_linked"
    assert audit.user_id == "email:abc"
    # No new users row, and the account's own name / address stay.
    assert not any(type(o).__name__ == "User" for o in added)
    assert account.name == "Person" and account.email == "person@gmail.com"
    db.commit.assert_awaited_once()
    notify.assert_called_once()
    assert notify.call_args.kwargs["user_id"] == "email:abc"
    assert notify.call_args.kwargs["event"] == "sign_in_method_auto_linked"


@pytest.mark.asyncio
async def test_case_variant_from_the_idp_still_links() -> None:
    account = _email_account()
    db = _db(*_sequence([account]), _result(first=None))
    role, _ = await _sign_in(db, email="Person@Gmail.com")
    assert role == Role.USER


@pytest.mark.parametrize(
    ("overrides", "kwargs"),
    [
        ({}, {"authoritative": False}),
        ({}, {"provider": "github", "authoritative": False}),
        ({"totp_enabled": True}, {}),
        ({"role": "admin"}, {}),
        ({"provider_auto_link_blocked_at": datetime(2026, 2, 1)}, {}),
        ({"login_id": "admin"}, {}),
        ({"auth_method": "oauth"}, {}),
        ({"email_verified_at": None}, {}),
        ({"email": "Person@gmail.com"}, {}),
    ],
)
@pytest.mark.asyncio
async def test_every_refusal_is_email_in_use_and_inserts_nothing(overrides, kwargs) -> None:
    db = _db(*_sequence([_email_account(**overrides)]))
    with pytest.raises(ConflictError):
        await _sign_in(db, **kwargs)
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_provider_of_that_kind_already_linked_is_refused() -> None:
    db = _db(*_sequence([_email_account()], already_linked=(1,)))
    with pytest.raises(ConflictError):
        await _sign_in(db)
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_several_holders_are_refused() -> None:
    db = _db(*_sequence([_email_account(), _email_account(id=8, user_id="email:def")]))
    with pytest.raises(ConflictError):
        await _sign_in(db)
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_non_ascii_idp_address_is_refused() -> None:
    db = _db(*_sequence([_email_account(email="person@gmail.com")]))
    with pytest.raises(ConflictError):
        await _sign_in(db, email="person@gmaıl.com")
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_the_identitys_own_removed_account_falls_through_to_the_insert_arms() -> None:
    """A row whose user_id is the sub keeps today's ``provider_unlinked`` answer."""
    own = _email_account(user_id="google-sub-1", auth_method="oauth")
    db = _db(*_sequence([own]))
    db.commit = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("dup user_id")))
    db.execute.side_effect = list(db.execute.side_effect) + [
        _result(one=own),  # retry re-lookup by user_id
        _result(one=None),  # no own link row -> refused
    ]
    from utils.exceptions import UnlinkedProviderSignInError

    with pytest.raises(UnlinkedProviderSignInError):
        await _sign_in(db)


@pytest.mark.asyncio
async def test_two_tabs_linking_the_same_identity_sign_in_through_the_winner() -> None:
    account = _email_account()
    winner_link = SimpleNamespace(user_id="email:abc", last_used_at=None)
    db = _db(
        *_sequence([account]),
        _result(first=None),  # no google link yet
        _result(one=winner_link),  # after rollback: the other tab's link row
        _result(one=account),  # the owner
    )
    db.commit = AsyncMock(side_effect=[IntegrityError("INSERT", {}, Exception("dup")), None])
    role, notify = await _sign_in(db)
    assert role == Role.USER
    db.rollback.assert_awaited_once()
    assert winner_link.last_used_at is not None
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_no_holder_inserts_a_new_account_as_before() -> None:
    db = _db(*_sequence([]))
    role, _ = await _sign_in(db, email="new@gmail.com")
    assert role == Role.USER
    added = [c.args[0] for c in db.add.call_args_list]
    assert any(type(o).__name__ == "User" for o in added)
