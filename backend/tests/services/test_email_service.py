"""Tests for the EmailService Protocol's logging stub (Issue #478).

The Resend integration tests live in
``tests/services/email_providers/test_resend.py``. Tests here cover only:

1. ``LoggingEmailService`` writes the expected structured log fields and
   never logs the raw confirmation token or the confirm_url (which
   embeds the token).
2. ``reset_email_service_for_testing`` drops the singleton so a switch
   of ``EMAIL_PROVIDER`` between tests rebuilds the right backend.
3. ``get_email_service`` fails fast at boot when ``EMAIL_PROVIDER=resend``
   is set without a key (per Issue #478 design).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import services.email_service as email_service_module
from services.email_service import (
    LoggingEmailService,
    get_email_service,
    reset_email_service_for_testing,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Each test starts and ends with no live email_service singleton."""
    reset_email_service_for_testing()
    yield
    reset_email_service_for_testing()


# ---------------------------------------------------------------------------
# LoggingEmailService — structured-log discipline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logging_send_erasure_receipt_logs_expected_fields():
    svc = LoggingEmailService()
    with patch.object(email_service_module, "logger") as mock_logger:
        result = await svc.send_erasure_receipt(
            to_email="user@example.com",
            request_id="req-123",
        )

    assert result is True
    mock_logger.info.assert_called_once()
    args, kwargs = mock_logger.info.call_args
    assert args[0] == "erasure_email_receipt"
    assert kwargs == {
        "to_email": "user@example.com",
        "request_id": "req-123",
        "email_dispatch_required": True,
        "template": "erasure_receipt",
    }


@pytest.mark.asyncio
async def test_logging_send_erasure_cooling_off_logs_scheduled_for():
    svc = LoggingEmailService()
    with patch.object(email_service_module, "logger") as mock_logger:
        result = await svc.send_erasure_cooling_off_started(
            to_email="user@example.com",
            request_id="req-456",
            scheduled_for_iso="2026-05-05T12:00:00Z",
        )

    assert result is True
    args, kwargs = mock_logger.info.call_args
    assert args[0] == "erasure_email_cooling_off_started"
    assert kwargs["scheduled_for"] == "2026-05-05T12:00:00Z"


@pytest.mark.asyncio
async def test_logging_send_erasure_complete_logs_expected_fields():
    svc = LoggingEmailService()
    with patch.object(email_service_module, "logger") as mock_logger:
        result = await svc.send_erasure_complete(
            to_email="user@example.com",
            request_id="req-789",
        )

    assert result is True
    args, kwargs = mock_logger.info.call_args
    assert args[0] == "erasure_email_complete"
    assert kwargs == {
        "to_email": "user@example.com",
        "request_id": "req-789",
        "email_dispatch_required": True,
        "template": "erasure_complete",
    }


@pytest.mark.asyncio
async def test_logging_send_erasure_confirmation_does_not_log_raw_token():
    """Critical Issue #478 invariant: the LoggingEmailService stub MUST
    NOT write the raw token (or the confirm_url that embeds it) to the
    structured log. Today the stub is the closed-beta default; if a
    future refactor accidentally surfaces the token, the gating work in
    #469 stops protecting OAuth users.
    """
    svc = LoggingEmailService()
    raw_token = "raw-secret-confirm-token-do-not-leak-7f3a"  # noqa: S105
    confirm_url = f"https://app.example.com/account/erasure/confirm?token={raw_token}"

    with patch.object(email_service_module, "logger") as mock_logger:
        result = await svc.send_erasure_confirmation(
            to_email="user@example.com",
            request_id="req-999",
            confirm_token=raw_token,
            confirm_url=confirm_url,
        )

    assert result is True
    mock_logger.info.assert_called_once()
    args, kwargs = mock_logger.info.call_args

    # Event name + non-sensitive fields are present.
    assert args[0] == "erasure_email_confirmation"
    assert kwargs["to_email"] == "user@example.com"
    assert kwargs["request_id"] == "req-999"
    assert kwargs["template"] == "erasure_confirmation"
    assert kwargs["email_dispatch_required"] is True

    # Raw token and confirm_url are NEVER in any log field — neither in
    # keys nor in values. The haystack flattens kwargs into both keys and
    # values so a future regression that names a kwarg literally after
    # the token (e.g. an accidental ``token=raw_token`` kwarg) is caught
    # the same way a value-side leak would be.
    haystack = " ".join(
        str(part)
        for part in (
            *args,
            *(item_part for item in kwargs.items() for item_part in item),
        )
    )
    assert raw_token not in haystack, "raw confirm_token leaked into log payload"
    assert confirm_url not in haystack, "confirm_url leaked into log payload"


# ---------------------------------------------------------------------------
# Singleton + provider switch
# ---------------------------------------------------------------------------


def test_get_email_service_returns_logging_by_default(monkeypatch: pytest.MonkeyPatch):
    """With no env override, EMAIL_PROVIDER defaults to 'logging' and the
    singleton is a LoggingEmailService."""
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    # Settings is itself a module-level singleton — reset it too.
    import config.settings as settings_module

    settings_module._settings = None

    svc = get_email_service()
    assert isinstance(svc, LoggingEmailService)


def test_get_email_service_returns_singleton(monkeypatch: pytest.MonkeyPatch):
    """Successive calls return the same instance until reset."""
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    import config.settings as settings_module

    settings_module._settings = None

    first = get_email_service()
    second = get_email_service()
    assert first is second


def test_get_email_service_resend_without_api_key_raises(monkeypatch: pytest.MonkeyPatch):
    """EMAIL_PROVIDER=resend with no RESEND_API_KEY must fail fast at Settings load.

    Settings._validate_resend_config raises a ``ValueError`` inside a
    ``model_validator(mode="after")``; pydantic surfaces it as a
    ``ValidationError`` whose message embeds the original ``ValueError``
    text, so the ``match=`` regex still binds against the meaningful token.
    """
    from pydantic import ValidationError

    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    import config.settings as settings_module

    settings_module._settings = None

    with pytest.raises(ValidationError, match="RESEND_API_KEY"):
        get_email_service()


def test_get_email_service_resend_with_blank_from_email_raises(monkeypatch: pytest.MonkeyPatch):
    """Whitespace-only RESEND_FROM_EMAIL must fail at Settings load too —
    the validator strips before checking, so ``"   "`` is rejected the same
    way an empty string would be."""
    from pydantic import ValidationError

    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key_12345")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "   ")
    import config.settings as settings_module

    settings_module._settings = None

    with pytest.raises(ValidationError, match="RESEND_FROM_EMAIL"):
        get_email_service()


def test_reset_email_service_for_testing_drops_singleton(monkeypatch: pytest.MonkeyPatch):
    """Calling reset_email_service_for_testing must rebuild on the next call."""
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    import config.settings as settings_module

    settings_module._settings = None

    first = get_email_service()
    reset_email_service_for_testing()
    second = get_email_service()
    assert first is not second
