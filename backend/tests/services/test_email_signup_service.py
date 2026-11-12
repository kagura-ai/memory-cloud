"""EmailSignupService without a database (#1734).

Pinned here: what may refuse before the link is spent (password policy,
terms) and before bcrypt runs (an unknown token), that sending never raises
and never logs the link, that the background task swallows errors, and the
address validator. Request / complete against Postgres:
``tests/integration/test_email_signup_db.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.email_signup_service as svc_module
from services.email_signup_service import (
    AccountExistsNotice,
    EmailSignupService,
    PendingSignupEmail,
    SignupConsents,
    process_signup_request,
    validate_signup_address,
)
from utils.exceptions import PasswordLinkInvalidError, ValidationError

GOOD_PASSWORD = "Correct-Horse-Battery-42"


def _service(db=None, email_service=None) -> EmailSignupService:
    db = db or MagicMock()
    db.execute = getattr(db, "execute", None) or AsyncMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    return EmailSignupService(db, email_service=email_service or MagicMock())


class TestCompleteRefusesBeforeSpendingTheLink:
    @pytest.mark.asyncio
    async def test_password_policy_first(self) -> None:
        svc = _service()
        svc._live_request = AsyncMock()
        with pytest.raises(ValidationError) as info:
            await svc.complete(
                raw_token="t", new_password="x", name=None, consents=SignupConsents(None)
            )
        assert info.value.details.get("field") == "new_password"
        svc._live_request.assert_not_awaited()
        svc.db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_terms_before_the_link(self, monkeypatch) -> None:
        monkeypatch.setattr(svc_module, "current_terms_version", lambda: "2026-01")
        svc = _service()
        svc._live_request = AsyncMock()
        with pytest.raises(ValidationError) as info:
            await svc.complete(
                raw_token="t",
                new_password=GOOD_PASSWORD,
                name=None,
                consents=SignupConsents(terms_version="2025-12"),
            )
        assert info.value.details.get("field") == "accepted_terms"
        svc._live_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_token_costs_no_bcrypt(self, monkeypatch) -> None:
        monkeypatch.setattr(svc_module, "current_terms_version", lambda: None)
        hashed = MagicMock()
        monkeypatch.setattr(svc_module, "hash_password", hashed)
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        svc = _service(db)
        with pytest.raises(PasswordLinkInvalidError):
            await svc.complete(
                raw_token="nope",
                new_password=GOOD_PASSWORD,
                name=None,
                consents=SignupConsents(None),
            )
        hashed.assert_not_called()


class TestSend:
    @pytest.mark.asyncio
    async def test_never_raises_and_never_logs_the_link(self) -> None:
        email_service = MagicMock()
        email_service.send_signup_verification = AsyncMock(side_effect=RuntimeError("boom tok_X"))
        svc = _service(email_service=email_service)
        with patch.object(svc_module, "logger") as log:
            await svc.send(
                PendingSignupEmail(
                    to_email="a@example.test",
                    verify_url="https://app.example.test/signup/verify?token=tok_X",
                    expires_in_minutes=60,
                    locale=None,
                )
            )
        assert "tok_X" not in repr(log.mock_calls)
        assert "a@example.test" not in repr(log.mock_calls)

    @pytest.mark.asyncio
    async def test_notice_points_at_login_and_reset(self, monkeypatch) -> None:
        email_service = MagicMock()
        email_service.send_signup_account_exists = AsyncMock(return_value=True)
        svc = _service(email_service=email_service)
        await svc.send(AccountExistsNotice(to_email="a@example.test", locale="ja"))
        kwargs = email_service.send_signup_account_exists.await_args.kwargs
        assert kwargs["sign_in_url"].endswith("/login")
        assert kwargs["reset_url"].endswith("/password/forgot")
        assert kwargs["locale"] == "ja"


class TestBackgroundTask:
    @pytest.mark.asyncio
    async def test_swallows_errors_and_logs_the_type_only(self) -> None:
        factory = MagicMock(side_effect=RuntimeError("connection to a@example.test failed"))
        with patch.object(svc_module, "logger") as log:
            await process_signup_request(
                email="a@example.test",
                beta_invite_token_hash=None,
                locale=None,
                timezone=None,
                session_factory=factory,
            )
        assert "a@example.test" not in repr(log.mock_calls)
        log.error.assert_called_once()


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("A@Example.com", "a@example.com"),
        (" first.last+tag@sub.example.co.jp ", "first.last+tag@sub.example.co.jp"),
        ("x@xn--bcher-kva.example", "x@xn--bcher-kva.example"),
    ],
)
def test_validator_accepts_and_normalizes(raw: str, normalized: str) -> None:
    assert validate_signup_address(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "plain",
        "a@b",
        "a@b.c1",
        ".a@example.com",
        "a.@example.com",
        "a..b@example.com",
        "a@example..com",
        "a@exa_mple.com",
        "a\t@example.com",
        "a\x00@example.com",
        "a<b>@example.com",
        "a(b)@example.com",
        "a,b@example.com",
        "x" * 65 + "@example.com",
        "a@" + "b" * 250 + ".com",
        "me@local",
        "me@LOCAL",
    ],
)
def test_validator_refuses_bad_shapes(raw: str) -> None:
    with pytest.raises(ValueError):
        validate_signup_address(raw)
