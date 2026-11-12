"""``ensure_user(announcement_consent=...)`` on a mocked session (Issue #2761).

The sign-up opt-in is added in the unit of work that creates the account, after
the user and its provider link, and nowhere else:

- a new account: one ``signup`` row, added before the single commit;
- no answer: nothing added beyond the user and the link;
- an existing account and the ``user_id`` race retry: no row (the account keeps
  its own answer).

The same paths run on Postgres in
``tests/integration/test_role_manager_signup_consent_db.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from models.announcement_consent import AnnouncementEmailConsent
from models.auth import User, UserOAuthProvider
from services.announcement_consent_service import SignupAnnouncementConsent
from tests.auth.test_role_manager_postgres import (
    _LOCK,
    _NO_HOLDERS,
    _execute_returns,
    _make_db_mock,
    _oauth_link_row,
    _patch_get_db,
    _user_id_unique_violation,
    _user_row,
    role_manager,  # noqa: F401  (fixture)
)

YES = SignupAnnouncementConsent(value=True, wording_version="2026-11")


def _added(db) -> list[object]:
    return [call.args[0] for call in db.add.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False])
async def test_new_account_adds_the_row_with_the_user(role_manager, value) -> None:  # noqa: F811
    db = _make_db_mock(_execute_returns(None, {"scalar": 3}, _LOCK, _NO_HOLDERS))

    with _patch_get_db(db):
        await role_manager.ensure_user(
            email="new@example.com",
            user_id="g-new",
            auth_provider="google",
            email_verified=True,
            announcement_consent=SignupAnnouncementConsent(value=value, wording_version="2026-11"),
        )

    added = _added(db)
    assert [type(a) for a in added] == [User, UserOAuthProvider, AnnouncementEmailConsent]
    consent = added[2]
    assert isinstance(consent, AnnouncementEmailConsent)
    assert (consent.user_id, consent.value, consent.source, consent.wording_version) == (
        "g-new",
        value,
        "signup",
        "2026-11",
    )
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_answer_adds_no_row(role_manager) -> None:  # noqa: F811
    db = _make_db_mock(_execute_returns(None, {"scalar": 3}, _LOCK, _NO_HOLDERS))

    with _patch_get_db(db):
        await role_manager.ensure_user(
            email="new@example.com",
            user_id="g-new",
            auth_provider="google",
            email_verified=True,
        )

    assert [type(a) for a in _added(db)] == [User, UserOAuthProvider]


@pytest.mark.asyncio
async def test_existing_account_adds_no_row(role_manager) -> None:  # noqa: F811
    existing = _user_row(email="alice@example.com")
    db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))

    with _patch_get_db(db):
        await role_manager.ensure_user(
            email="alice@example.com",
            user_id="u1",
            auth_provider="google",
            email_verified=True,
            announcement_consent=YES,
        )

    assert not any(isinstance(a, AnnouncementEmailConsent) for a in _added(db))


@pytest.mark.asyncio
async def test_user_id_race_retry_adds_no_second_row(role_manager) -> None:  # noqa: F811
    """The rolled-back first attempt carried the row; the retry signs in to the
    row a concurrent first sign-in created and adds nothing."""
    race_existing = _user_row(email="alice@example.com", name="Alice")
    db = _make_db_mock(
        _execute_returns(None, {"scalar": 0}, _LOCK, _NO_HOLDERS, race_existing, _oauth_link_row())
    )
    db.commit = AsyncMock(side_effect=[_user_id_unique_violation(), None])

    with _patch_get_db(db):
        await role_manager.ensure_user(
            email="alice@example.com",
            user_id="u1",
            name="Alice",
            auth_provider="google",
            email_verified=True,
            announcement_consent=YES,
        )

    db.rollback.assert_awaited_once()
    consents = [a for a in _added(db) if isinstance(a, AnnouncementEmailConsent)]
    assert len(consents) == 1  # only the attempt that was rolled back
