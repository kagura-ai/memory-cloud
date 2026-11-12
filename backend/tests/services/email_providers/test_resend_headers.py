"""``ResendEmailService._send`` header passthrough (Issue #2761).

Announcement mail must carry ``List-Unsubscribe`` / ``List-Unsubscribe-Post``.
``_send`` passes server-built headers to the SDK only when given (every
existing service notice keeps its exact payload) and refuses a header whose
name or value carries CR or LF (header injection) without calling the SDK.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.email_providers.resend import ResendEmailService


@pytest.fixture
def service() -> ResendEmailService:
    return ResendEmailService(api_key="re_test", from_email="noreply@example.com")


async def _send(service: ResendEmailService, **kwargs) -> bool:
    return await service._send(
        to_email="a@example.com",
        subject="s",
        text="t",
        log_event="test_mail",
        log_context={},
        **kwargs,
    )


@pytest.mark.asyncio
async def test_without_headers_the_payload_is_unchanged(service) -> None:
    with patch("resend.Emails.send", return_value={"id": "m1"}) as send:
        assert await _send(service) is True

    params = send.call_args.args[0]
    assert set(params) == {"from", "to", "subject", "text"}


@pytest.mark.asyncio
async def test_headers_are_passed_through(service) -> None:
    headers = {
        "List-Unsubscribe": "<https://app.example.com/api/v1/announcement-emails/unsubscribe/x>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    with patch("resend.Emails.send", return_value={"id": "m1"}) as send:
        assert await _send(service, headers=headers) is True

    assert send.call_args.args[0]["headers"] == headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"List-Unsubscribe": "<https://a.example.com/x>\r\nBcc: victim@example.com"},
        {"List-Unsubscribe": "<https://a.example.com/x>\nX: y"},
        {"X-Bad\r\nBcc": "v"},
        {"": "v"},
        {"List-Unsubscribe": 5},
    ],
)
async def test_a_header_with_cr_or_lf_is_refused(service, headers) -> None:
    with patch("resend.Emails.send", return_value={"id": "m1"}) as send:
        assert await _send(service, headers=headers) is False

    send.assert_not_called()
