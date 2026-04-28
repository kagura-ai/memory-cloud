"""Resend transactional email provider (Issue #478).

Wraps the synchronous ``resend`` Python SDK in ``asyncio.to_thread`` to
avoid blocking the FastAPI event loop, mirroring the discipline adopted
for ``stripe-python`` in PR #477 (commit ``5bdcf12``). Each ``EmailService``
method:

1. Builds the email body (plain text — templated HTML is a follow-up).
2. Hands the synchronous ``resend.Emails.send`` call off to a thread.
3. Catches every SDK exception, logs structured metadata (no raw token,
   no email body), and returns ``False`` per the Protocol's "do not raise"
   contract.

Outbound logs intentionally omit the email body and the confirm URL.
The send_erasure_confirmation flow puts the raw token inside ``confirm_url``
as a query parameter, so logging the body or the URL would defeat the
whole point of the gating work in #469.
"""

from __future__ import annotations

import asyncio
from typing import Any

import resend

from utils.logger import get_logger

logger = get_logger(__name__)


class ResendEmailService:
    """``EmailService`` backend backed by the Resend HTTPS API.

    Constructed by ``get_email_service()`` when ``EMAIL_PROVIDER=resend``.
    Configuration validation (presence of the API key) lives at the call
    site so that misconfiguration fails fast at boot, not at first send.
    """

    def __init__(self, *, api_key: str, from_email: str) -> None:
        if not api_key:
            raise ValueError("ResendEmailService requires a non-empty api_key")
        normalized_from_email = (from_email or "").strip()
        if not normalized_from_email:
            raise ValueError("ResendEmailService requires a non-empty from_email")
        # The resend SDK uses module-level state — assigning api_key here
        # is process-wide. Idempotent across constructor calls; the last
        # writer wins. Tests that need a clean slate should reset both
        # this attribute and the email_service singleton via
        # services.email_service.reset_email_service_for_testing.
        resend.api_key = api_key
        self._from_email = normalized_from_email

    async def _send(
        self,
        *,
        to_email: str,
        subject: str,
        text: str,
        log_event: str,
        log_context: dict[str, Any],
    ) -> bool:
        params: dict[str, Any] = {
            "from": self._from_email,
            "to": [to_email],
            "subject": subject,
            "text": text,
        }
        try:
            response = await asyncio.to_thread(resend.Emails.send, params)
        except Exception as exc:
            # Only log the exception type and a short stringified summary —
            # Resend SDK exceptions surface API status + reason ("Domain
            # not found", "Invalid API key", etc.) and do not echo the
            # request body, but we keep the message bounded as defense
            # in depth.
            logger.warning(
                f"{log_event}_failed",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
                **log_context,
            )
            return False

        message_id = response.get("id") if isinstance(response, dict) else None
        if not message_id:
            logger.warning(
                f"{log_event}_no_id",
                response_shape=type(response).__name__,
                **log_context,
            )
            return False

        logger.info(
            f"{log_event}_sent",
            resend_message_id=message_id,
            **log_context,
        )
        return True

    async def send_erasure_receipt(self, *, to_email: str, request_id: str) -> bool:
        text = (
            "We have received your account erasure request.\n"
            "\n"
            f"Request ID: {request_id}\n"
            "\n"
            "What happens next:\n"
            "  1. You confirm the request through a separate confirmation step.\n"
            "  2. After confirmation, a 7-day cooling-off period begins. You can\n"
            "     cancel during this window.\n"
            "  3. Once the cooling-off period ends, your account and personal\n"
            "     data are permanently deleted.\n"
            "\n"
            "If you did not initiate this request, contact support immediately.\n"
        )
        return await self._send(
            to_email=to_email,
            subject="Account erasure request received",
            text=text,
            log_event="erasure_email_receipt",
            log_context={
                "to_email": to_email,
                "request_id": request_id,
                "template": "erasure_receipt",
            },
        )

    async def send_erasure_cooling_off_started(
        self,
        *,
        to_email: str,
        request_id: str,
        scheduled_for_iso: str,
    ) -> bool:
        text = (
            "Your account erasure has entered the 7-day cooling-off period.\n"
            "\n"
            f"Request ID: {request_id}\n"
            f"Scheduled deletion: {scheduled_for_iso}\n"
            "\n"
            "If you change your mind, sign in and cancel the request before\n"
            "the scheduled date. After that point the deletion is permanent\n"
            "and cannot be undone.\n"
        )
        return await self._send(
            to_email=to_email,
            subject="Account erasure: 7-day cooling-off period started",
            text=text,
            log_event="erasure_email_cooling_off_started",
            log_context={
                "to_email": to_email,
                "request_id": request_id,
                "scheduled_for": scheduled_for_iso,
                "template": "erasure_cooling_off_started",
            },
        )

    async def send_erasure_complete(self, *, to_email: str, request_id: str) -> bool:
        text = (
            "Your account and personal data have been permanently deleted.\n"
            "\n"
            f"Request ID (kept for audit purposes only): {request_id}\n"
            "\n"
            "This is the final email you will receive from us regarding this\n"
            "account. Thank you for using Kagura.\n"
        )
        return await self._send(
            to_email=to_email,
            subject="Account erasure complete",
            text=text,
            log_event="erasure_email_complete",
            log_context={
                "to_email": to_email,
                "request_id": request_id,
                "template": "erasure_complete",
            },
        )

    async def send_erasure_confirmation(
        self,
        *,
        to_email: str,
        request_id: str,
        confirm_token: str,
        confirm_url: str,
    ) -> bool:
        # confirm_url already embeds the token as a query parameter. Putting
        # the bare token in the body separately would just give the recipient
        # two copies of the same secret. We keep confirm_token in the
        # signature for forward compatibility with template engines that
        # render the token outside a URL (e.g. one-time codes), and discard
        # it here so it cannot accidentally leak into the body or logs.
        del confirm_token

        text = (
            "Your account erasure request needs confirmation.\n"
            "\n"
            "Click the link below within 1 hour to proceed. After 1 hour the\n"
            "link expires and you will need to start a new erasure request.\n"
            "\n"
            f"  {confirm_url}\n"
            "\n"
            f"Request ID: {request_id}\n"
            "\n"
            "If you did not initiate this request, ignore this email — the\n"
            "link will expire on its own and no further action will be taken.\n"
        )
        return await self._send(
            to_email=to_email,
            subject="Confirm your account erasure request",
            text=text,
            log_event="erasure_email_confirmation",
            log_context={
                "to_email": to_email,
                "request_id": request_id,
                "template": "erasure_confirmation",
            },
        )
