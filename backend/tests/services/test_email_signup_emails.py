"""Email sign-up mails (#1734): the link goes in the body only, never in a log line."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import services.email_providers.resend as resend_module
import services.email_service as email_service_module
from services.email_providers.resend import ResendEmailService
from services.email_service import LoggingEmailService

_TOKEN = "tok_" + "V" * 40
_URL = f"https://app.example.test/signup/verify?token={_TOKEN}"


@pytest.mark.asyncio
async def test_logging_backend_never_logs_the_link_or_the_address() -> None:
    with patch.object(email_service_module, "logger") as log:
        ok = await LoggingEmailService().send_signup_verification(
            to_email="Person@Example.com", verify_url=_URL, expires_in_minutes=60, locale="ja"
        )
        ok2 = await LoggingEmailService().send_signup_account_exists(
            to_email="Person@Example.com",
            sign_in_url="https://app.example.test/login",
            reset_url="https://app.example.test/password/forgot",
            locale=None,
        )
    assert ok is True and ok2 is True
    logged = repr(log.mock_calls)
    assert _TOKEN not in logged
    assert "signup/verify" not in logged
    assert "person@example.com" not in logged.lower()
    templates = [c.kwargs["template"] for c in log.info.call_args_list]
    assert templates == ["signup_verification", "signup_account_exists"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "subject_part", "expiry"),
    [(None, "Finish creating", "60 minutes"), ("ja", "登録を完了", "60 分")],
)
async def test_resend_verification_carries_the_link_in_the_body_only(
    locale: str | None, subject_part: str, expiry: str
) -> None:
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    with (
        patch.object(resend_module.resend.Emails, "send", return_value={"id": "re_1"}) as send,
        patch.object(resend_module, "logger") as log,
    ):
        ok = await svc.send_signup_verification(
            to_email="user@example.com", verify_url=_URL, expires_in_minutes=60, locale=locale
        )
    assert ok is True
    (params,), _ = send.call_args
    assert params["to"] == ["user@example.com"]
    assert subject_part in params["subject"]
    assert _URL in params["text"]
    assert expiry in params["text"]
    logged = repr(log.mock_calls)
    assert _TOKEN not in logged
    assert "user@example.com" not in logged


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", [None, "ja"])
async def test_resend_account_exists_has_no_token_and_names_every_way_in(
    locale: str | None,
) -> None:
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    with (
        patch.object(resend_module.resend.Emails, "send", return_value={"id": "re_2"}) as send,
        patch.object(resend_module, "logger") as log,
    ):
        ok = await svc.send_signup_account_exists(
            to_email="user@example.com",
            sign_in_url="https://app.example.test/login",
            reset_url="https://app.example.test/password/forgot",
            locale=locale,
        )
    assert ok is True
    (params,), _ = send.call_args
    text = params["text"]
    assert "https://app.example.test/login" in text
    assert "https://app.example.test/password/forgot" in text
    assert "Google" in text and "GitHub" in text
    assert "token" not in text
    assert "user@example.com" not in repr(log.mock_calls)
