"""The announcement-email opt-in on the email sign-up completion (#1734 + #2761).

The box is on ``/signup/verify``: the person who ticks it opened the emailed
link, so the answer travels with ``complete`` only, never with ``request``.
Pinned here without a database:

- ``EmailSignupCompleteBody`` takes ``announcement_emails`` and
  ``announcement_consent_version`` (64 characters at most); the request body
  has neither;
- the route resolves them with ``resolve_signup_consent`` into
  ``SignupConsents.announcement`` (an absent, invalid or stale answer, or the
  feature off, is ``None``);
- ``stage_signup_consents`` adds one ``signup`` row for an answer and nothing
  without one, and never commits.

The rows against Postgres: ``tests/integration/test_email_signup_announcement_db.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError as PydanticValidationError

from api.routes import email_signup as routes
from auth.roles import Role
from config.settings import get_settings
from models.announcement_consent import AnnouncementEmailConsent
from services.announcement_consent_service import (
    ANNOUNCEMENT_CONSENT_WORDING_VERSION as V,
)
from services.announcement_consent_service import SignupAnnouncementConsent
from services.email_signup_service import (
    CreatedEmailAccount,
    SignupConsents,
    stage_signup_consents,
)


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="192.0.2.10"), headers={"user-agent": "pytest"}
    )


class TestBodies:
    def test_complete_takes_the_answer_and_its_version(self) -> None:
        body = routes.EmailSignupCompleteBody(
            token="t",
            new_password="pw",
            announcement_emails=True,
            announcement_consent_version=V,
        )
        assert (body.announcement_emails, body.announcement_consent_version) == (True, V)
        bare = routes.EmailSignupCompleteBody(token="t", new_password="pw")
        assert (bare.announcement_emails, bare.announcement_consent_version) == (None, None)

    def test_version_is_capped(self) -> None:
        with pytest.raises(PydanticValidationError):
            routes.EmailSignupCompleteBody(
                token="t", new_password="pw", announcement_consent_version="x" * 65
            )

    def test_request_has_no_answer(self) -> None:
        fields = set(routes.EmailSignupRequestBody.model_fields)
        assert "announcement_emails" not in fields
        assert "announcement_consent_version" not in fields


class TestRouteResolvesTheAnswer:
    @pytest.fixture
    def service(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_email_signup", True)

        async def _incr(key: str, ttl: int | None = None) -> int:
            return 1

        monkeypatch.setattr(routes, "increment_counter", _incr)
        account = CreatedEmailAccount(
            user_id="email:abc",
            email="a@example.test",
            name=None,
            role=Role.USER,
            admission="open_registration",
        )
        service = MagicMock()
        service.complete = AsyncMock(return_value=account)
        monkeypatch.setattr(routes, "EmailSignupService", MagicMock(return_value=service))
        monkeypatch.setattr(routes, "ensure_first_workspace", AsyncMock())
        monkeypatch.setattr(routes, "note_browser_sign_in", AsyncMock())
        monkeypatch.setattr(
            routes.auth_module, "_create_password_session", AsyncMock(return_value="s")
        )
        monkeypatch.setattr(routes.auth_module, "_set_session_cookie", MagicMock())
        return service

    async def _consents(self, service, **fields) -> SignupConsents:
        await routes.complete_email_signup(
            routes.EmailSignupCompleteBody(token="t", new_password="pw", **fields),
            _request(),
            db=MagicMock(),
        )
        return service.complete.await_args.kwargs["consents"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, False])
    async def test_current_version_is_recorded(self, service, monkeypatch, value) -> None:
        monkeypatch.setattr(get_settings(), "announcement_emails_enabled", True)
        consents = await self._consents(
            service,
            accepted_terms="2026-01",
            announcement_emails=value,
            announcement_consent_version=V,
        )
        assert consents.terms_version == "2026-01"
        assert consents.announcement == SignupAnnouncementConsent(value=value, wording_version=V)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fields",
        [
            {},
            {"announcement_emails": True},
            {"announcement_emails": True, "announcement_consent_version": "2000-01"},
            {"announcement_consent_version": V},
        ],
        ids=["absent", "no-version", "stale-version", "no-answer"],
    )
    async def test_nothing_without_a_valid_answer(self, service, monkeypatch, fields) -> None:
        monkeypatch.setattr(get_settings(), "announcement_emails_enabled", True)
        assert (await self._consents(service, **fields)).announcement is None

    @pytest.mark.asyncio
    async def test_nothing_while_the_feature_is_off(self, service, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "announcement_emails_enabled", False)
        consents = await self._consents(
            service, announcement_emails=True, announcement_consent_version=V
        )
        assert consents.announcement is None


class TestStageSignupConsents:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, False])
    async def test_an_answer_adds_one_signup_row(self, value) -> None:
        db = MagicMock()
        db.commit = AsyncMock()
        await stage_signup_consents(
            db,
            user_id="email:abc",
            consents=SignupConsents(
                terms_version=None,
                announcement=SignupAnnouncementConsent(value=value, wording_version=V),
            ),
        )
        db.add.assert_called_once()
        row = db.add.call_args.args[0]
        assert isinstance(row, AnnouncementEmailConsent)
        assert (row.user_id, row.value, row.source, row.wording_version) == (
            "email:abc",
            value,
            "signup",
            V,
        )
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_answer_adds_nothing(self) -> None:
        db = MagicMock()
        await stage_signup_consents(
            db, user_id="email:abc", consents=SignupConsents(terms_version="2026-01")
        )
        db.add.assert_not_called()
