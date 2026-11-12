"""The provider link confirmation mail (#2786): link in the body only, never logged."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import services.email_providers.resend as resend_module
import services.email_service as email_service_module
from services.email_providers.resend import ResendEmailService
from services.email_service import LoggingEmailService

_TOKEN = "T" * 43
_URL = f"https://app.example.test/link/confirm?token={_TOKEN}"


@pytest.mark.asyncio
async def test_logging_backend_never_logs_the_link_or_the_address() -> None:
    with patch.object(email_service_module, "logger") as log:
        ok = await LoggingEmailService().send_provider_link_confirmation(
            to_email="Person@Example.com",
            confirm_url=_URL,
            provider_label="GitHub",
            provider_login="octo-cat",
            expires_in_minutes=15,
            locale="ja",
        )
    assert ok is True
    logged = repr(log.mock_calls)
    assert _TOKEN not in logged
    assert "link/confirm" not in logged
    assert "person@example.com" not in logged.lower()
    assert log.info.call_args.kwargs["template"] == "provider_link_confirmation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "subject_part", "same_browser", "never_forward", "expiry"),
    [
        (None, "Connect your GitHub account", "same browser", "Never send this link", "15"),
        ("ja", "GitHub アカウント", "同じブラウザ", "転送しないでください", "15 分"),
    ],
)
async def test_resend_body_names_the_account_and_warns(
    locale, subject_part, same_browser, never_forward, expiry
) -> None:
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    with (
        patch.object(resend_module.resend.Emails, "send", return_value={"id": "re_1"}) as send,
        patch.object(resend_module, "logger") as log,
    ):
        ok = await svc.send_provider_link_confirmation(
            to_email="user@example.com",
            confirm_url=_URL,
            provider_label="GitHub",
            provider_login="octo-cat",
            expires_in_minutes=15,
            locale=locale,
        )
    assert ok is True
    (params,), _ = send.call_args
    text = params["text"]
    assert params["to"] == ["user@example.com"]
    assert subject_part in params["subject"]
    assert "octo-cat" in text
    assert _URL in text
    assert same_browser in text
    assert never_forward in text
    assert expiry in text
    # Plain text only: nothing to escape, no HTML part.
    assert "html" not in params
    logged = repr(log.mock_calls)
    assert _TOKEN not in logged
    assert "user@example.com" not in logged


@pytest.mark.asyncio
async def test_english_body_says_nobody_got_in() -> None:
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    with patch.object(resend_module.resend.Emails, "send", return_value={"id": "re_1"}) as send:
        await svc.send_provider_link_confirmation(
            to_email="user@example.com",
            confirm_url=_URL,
            provider_label="GitHub",
            provider_login="octo-cat",
            expires_in_minutes=15,
            locale=None,
        )
    text = send.call_args.args[0]["text"]
    assert "tried to sign in" in text
    assert "nothing was connected" in text
    assert "signed in to Kagura" not in text


@pytest.mark.asyncio
async def test_a_login_with_line_breaks_stays_on_one_line() -> None:
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    with patch.object(resend_module.resend.Emails, "send", return_value={"id": "re_1"}) as send:
        await svc.send_provider_link_confirmation(
            to_email="user@example.com",
            confirm_url=_URL,
            provider_label="GitHub",
            provider_login="a\r\nBcc: x@example.com",
            expires_in_minutes=15,
            locale=None,
        )
    params = send.call_args.args[0]
    assert "\r" not in params["subject"] and "\n" not in params["subject"]
    assert "a Bcc: x@example.com" in params["text"]
