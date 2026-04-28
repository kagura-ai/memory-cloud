"""Tests for ResendEmailService (Issue #478).

Covers:
- All 4 EmailService Protocol methods invoke ``resend.Emails.send`` with
  the expected payload shape.
- The synchronous SDK call runs on a worker thread (causal test, no
  wall-clock thresholds — mirrors PR #477's stripe_service test pattern).
- Hard failures from the SDK return ``False`` per the Protocol's "do not
  raise" contract; the failure log includes type + bounded message but
  never echoes the request body.
- ``send_erasure_confirmation`` does NOT write the raw token or the
  confirm_url into any log (parity with the LoggingEmailService check
  in tests/services/test_email_service.py).
- Constructor fails fast when api_key is empty.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

import services.email_providers.resend as resend_module
from services.email_providers.resend import ResendEmailService

# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_constructor_rejects_empty_api_key():
    with pytest.raises(ValueError, match="api_key"):
        ResendEmailService(api_key="", from_email="noreply@example.com")


def test_constructor_rejects_blank_from_email():
    """Whitespace-only ``from_email`` is fail-fast — the constructor
    strips before checking, so accidentally setting ``RESEND_FROM_EMAIL=""``
    or ``"   "`` raises rather than constructing a service that would
    later 4xx at first send.
    """
    with pytest.raises(ValueError, match="from_email"):
        ResendEmailService(api_key="re_test", from_email="")
    with pytest.raises(ValueError, match="from_email"):
        ResendEmailService(api_key="re_test", from_email="   ")


def test_constructor_strips_from_email_whitespace():
    """Surrounding whitespace on ``from_email`` is normalized — Resend
    rejects ``" noreply@... "`` and similar values, so trim before the
    SDK ever sees them."""
    svc = ResendEmailService(api_key="re_test", from_email="  noreply@example.com  ")
    assert svc._from_email == "noreply@example.com"


def test_constructor_sets_module_level_api_key():
    """Resend SDK uses module-level state. Constructor must propagate the
    api_key into that state so subsequent send calls authenticate.
    """
    ResendEmailService(api_key="re_test_abc123", from_email="noreply@example.com")
    assert resend_module.resend.api_key == "re_test_abc123"


# ---------------------------------------------------------------------------
# Happy path — all 4 methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_erasure_receipt_calls_sdk_with_expected_payload():
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")

    with patch.object(
        resend_module.resend.Emails, "send", return_value={"id": "re_msg_001"}
    ) as mock_send:
        result = await svc.send_erasure_receipt(
            to_email="user@example.com",
            request_id="req-123",
        )

    assert result is True
    mock_send.assert_called_once()
    (params,), _ = mock_send.call_args
    assert params["from"] == "noreply@example.com"
    assert params["to"] == ["user@example.com"]
    assert "Account erasure request received" in params["subject"]
    assert "req-123" in params["text"]


@pytest.mark.asyncio
async def test_send_erasure_cooling_off_started_includes_scheduled_for():
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")

    with patch.object(
        resend_module.resend.Emails, "send", return_value={"id": "re_msg_002"}
    ) as mock_send:
        result = await svc.send_erasure_cooling_off_started(
            to_email="user@example.com",
            request_id="req-456",
            scheduled_for_iso="2026-05-05T12:00:00Z",
        )

    assert result is True
    (params,), _ = mock_send.call_args
    assert "2026-05-05T12:00:00Z" in params["text"]
    assert "req-456" in params["text"]


@pytest.mark.asyncio
async def test_send_erasure_complete_includes_request_id():
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")

    with patch.object(
        resend_module.resend.Emails, "send", return_value={"id": "re_msg_003"}
    ) as mock_send:
        result = await svc.send_erasure_complete(
            to_email="user@example.com",
            request_id="req-789",
        )

    assert result is True
    (params,), _ = mock_send.call_args
    assert "req-789" in params["text"]


@pytest.mark.asyncio
async def test_send_erasure_confirmation_includes_confirm_url():
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    confirm_url = "https://app.example.com/account/erasure/confirm?token=raw-secret-7f3a"

    with patch.object(
        resend_module.resend.Emails, "send", return_value={"id": "re_msg_004"}
    ) as mock_send:
        result = await svc.send_erasure_confirmation(
            to_email="user@example.com",
            request_id="req-999",
            confirm_token="raw-secret-7f3a",  # noqa: S106
            confirm_url=confirm_url,
        )

    assert result is True
    (params,), _ = mock_send.call_args
    # The URL is in the email body — that is the whole point of this
    # method (recipient receives the confirmation link).
    assert confirm_url in params["text"]


# ---------------------------------------------------------------------------
# Failure handling — Protocol's "do not raise" contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_returns_false_when_sdk_raises():
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")

    with patch.object(
        resend_module.resend.Emails,
        "send",
        side_effect=RuntimeError("Domain not found"),
    ):
        result = await svc.send_erasure_receipt(
            to_email="user@example.com",
            request_id="req-fail",
        )

    assert result is False, "Protocol contract: must return False, not raise"


@pytest.mark.asyncio
async def test_send_returns_false_when_sdk_returns_no_id():
    """Resend without an ``id`` in the response means the email was not
    actually accepted — treat as failure rather than silently claiming
    success."""
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")

    with patch.object(resend_module.resend.Emails, "send", return_value={}):
        result = await svc.send_erasure_receipt(
            to_email="user@example.com",
            request_id="req-noid",
        )

    assert result is False


# ---------------------------------------------------------------------------
# Event-loop discipline (mirrors PR #477 stripe_service test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synchronous_sdk_call_runs_on_worker_thread():
    """Causal (not temporal) test: the wrapped SDK call must complete via
    a thread other than the asyncio event loop's thread. If it ran on
    the loop's thread, ``threading.current_thread()`` would equal the
    main thread — the assertion below catches that regression.
    """
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    captured: dict[str, threading.Thread] = {}

    def fake_send(_params: dict) -> dict:
        captured["thread"] = threading.current_thread()
        return {"id": "re_threaded"}

    with patch.object(resend_module.resend.Emails, "send", side_effect=fake_send):
        result = await svc.send_erasure_receipt(
            to_email="user@example.com",
            request_id="req-thread",
        )

    assert result is True
    assert captured["thread"] is not threading.main_thread(), (
        "resend.Emails.send must run on a worker thread, not the asyncio main thread"
    )


@pytest.mark.asyncio
async def test_synchronous_sdk_call_does_not_block_event_loop():
    """The wrapped sync call blocks until a concurrent asyncio task sets
    a flag and releases it. If the wrapper blocked the event loop, the
    threading.Event wait would time out and the test fails fast — no
    wall-clock thresholds.
    """
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    sync_started = threading.Event()
    async_progressed = threading.Event()
    allow_finish = threading.Event()

    def fake_send(_params: dict) -> dict:
        sync_started.set()
        assert allow_finish.wait(timeout=2), "sync call was never unblocked"
        assert async_progressed.is_set(), "concurrent asyncio task never made progress"
        return {"id": "re_unblocked"}

    async def unblock() -> None:
        started = await asyncio.to_thread(sync_started.wait, 2)
        assert started, "timed out waiting for sync send to start"
        await asyncio.sleep(0)
        async_progressed.set()
        allow_finish.set()

    with patch.object(resend_module.resend.Emails, "send", side_effect=fake_send):
        unblock_task = asyncio.create_task(unblock())
        result = await svc.send_erasure_receipt(
            to_email="user@example.com",
            request_id="req-nonblock",
        )
        await unblock_task

    assert result is True
    assert async_progressed.is_set()


# ---------------------------------------------------------------------------
# Confirmation flow — no token / URL leakage in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_erasure_confirmation_does_not_leak_token_in_logs():
    """Same invariant the LoggingEmailService check enforces, applied to
    the Resend backend's structured logs: neither the raw token nor the
    confirm_url may appear in any log payload.
    """
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    raw_token = "raw-secret-DO-NOT-LEAK-9c2e"  # noqa: S105
    confirm_url = f"https://app.example.com/account/erasure/confirm?token={raw_token}"

    with (
        patch.object(resend_module.resend.Emails, "send", return_value={"id": "re_ok"}),
        patch.object(resend_module, "logger") as mock_logger,
    ):
        result = await svc.send_erasure_confirmation(
            to_email="user@example.com",
            request_id="req-confirm",
            confirm_token=raw_token,
            confirm_url=confirm_url,
        )

    assert result is True

    # Concatenate every positional and keyword argument from every logger
    # call — info, warning, anything else — and verify the raw token /
    # URL are absent throughout.
    haystack_parts: list[str] = []
    for method_call in mock_logger.method_calls:
        # method_call is (method_name, args, kwargs)
        _name, args, kwargs = method_call
        haystack_parts.extend(str(a) for a in args)
        haystack_parts.extend(f"{k}={v}" for k, v in kwargs.items())
    haystack = " ".join(haystack_parts)

    assert raw_token not in haystack, "raw confirm_token leaked into logs"
    assert confirm_url not in haystack, "confirm_url leaked into logs"


@pytest.mark.asyncio
async def test_send_erasure_confirmation_failure_does_not_leak_token_in_logs():
    """When the SDK raises, the failure-log path runs. Verify token / URL
    redaction discipline holds on that branch too — that's the more
    failure-prone path and where leaks are easiest to slip in.
    """
    svc = ResendEmailService(api_key="re_test", from_email="noreply@example.com")
    raw_token = "raw-secret-DO-NOT-LEAK-fail-4a"  # noqa: S105
    confirm_url = f"https://app.example.com/account/erasure/confirm?token={raw_token}"

    with (
        patch.object(
            resend_module.resend.Emails,
            "send",
            side_effect=RuntimeError("API rate limit exceeded"),
        ),
        patch.object(resend_module, "logger") as mock_logger,
    ):
        result = await svc.send_erasure_confirmation(
            to_email="user@example.com",
            request_id="req-confirm-fail",
            confirm_token=raw_token,
            confirm_url=confirm_url,
        )

    assert result is False

    haystack_parts: list[str] = []
    for method_call in mock_logger.method_calls:
        _name, args, kwargs = method_call
        haystack_parts.extend(str(a) for a in args)
        haystack_parts.extend(f"{k}={v}" for k, v in kwargs.items())
    haystack = " ".join(haystack_parts)

    assert raw_token not in haystack, "raw confirm_token leaked into failure log"
    assert confirm_url not in haystack, "confirm_url leaked into failure log"
