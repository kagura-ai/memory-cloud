"""Email service interface for transactional notifications.

Issue #360: We need to send three account-erasure notifications (receipt,
cooling-off start, completion) to satisfy the GDPR Art.12(3) 1-month
response SLA. The codebase has no existing email infrastructure, so this
module ships a Protocol + a logging-only stub.

Operational reality (closed-beta scope):
    The active implementation is ``LoggingEmailService``, which writes a
    structured log line for every notification. ``docs/ops/erasure-runbook.md``
    instructs the on-call admin to forward those log entries from the email
    payload to ``support@`` for manual delivery, with an SLA of 1 business
    day on receipt notifications. This is the trade-off we accepted in the
    Phase-3 design review: shipping a real provider (Resend / SES /
    Postmark) is deferred until after Public Beta opens, so v0.14.1 doesn't
    block on vendor selection.

When swapping in a real provider, write a new class implementing
``EmailService`` and update ``get_email_service()`` to return it. The
caller code in ``AccountErasureService`` does not change.
"""

from __future__ import annotations

from typing import Protocol

from utils.logger import get_logger

logger = get_logger(__name__)


class EmailService(Protocol):
    """Transactional email sender.

    Implementations must NOT raise on send failure — they should log the
    failure and return False. The callers (notification flows) must be
    resilient to "email did not arrive" because the underlying account
    state is the source of truth, and the email is only a courtesy
    notification.
    """

    async def send_erasure_receipt(self, *, to_email: str, request_id: str) -> bool:
        """Confirm we received the erasure request (sent within 1 business day).

        Args:
            to_email: Recipient address (the user being erased).
            request_id: ``erasure_requests.id`` UUID, for cross-reference.

        Returns:
            True if dispatch succeeded (or was logged for manual handling),
            False on hard failure.
        """

    async def send_erasure_confirmation_link(
        self,
        *,
        to_email: str,
        request_id: str,
        confirm_url: str,
    ) -> bool:
        """Send the one-time confirmation link for OAuth users.

        Args:
            to_email: Recipient address.
            request_id: ``erasure_requests.id`` UUID.
            confirm_url: Full HTTPS URL with one-time token (expires in 1h).
        """

    async def send_erasure_cooling_off_started(
        self,
        *,
        to_email: str,
        request_id: str,
        scheduled_for_iso: str,
    ) -> bool:
        """Notify that the 7-day cooling-off period has begun.

        Args:
            to_email: Recipient address.
            request_id: ``erasure_requests.id`` UUID.
            scheduled_for_iso: ISO-8601 timestamp when erasure executes.
        """

    async def send_erasure_complete(self, *, to_email: str, request_id: str) -> bool:
        """Notify that the erasure has completed and the account is gone.

        This must be sent within 24 hours of completion (CLO follow-up #4
        in Issue #360 body).

        Args:
            to_email: Recipient address (still routable — this is the last
                contact we will have with the subject).
            request_id: ``erasure_requests.id`` UUID.
        """


class LoggingEmailService:
    """Default stub implementation: structured logs only, no SMTP.

    Each method writes one ``info`` log entry with a stable event name and
    every field an ops admin needs to forward the message manually. Search
    for ``email_dispatch_required=true`` in production logs to find the
    queue of notifications the admin must hand-deliver.
    """

    async def send_erasure_receipt(self, *, to_email: str, request_id: str) -> bool:
        logger.info(
            "erasure_email_receipt",
            to_email=to_email,
            request_id=request_id,
            email_dispatch_required=True,
            template="erasure_receipt",
        )
        return True

    async def send_erasure_confirmation_link(
        self,
        *,
        to_email: str,
        request_id: str,
        confirm_url: str,
    ) -> bool:
        logger.info(
            "erasure_email_confirmation_link",
            to_email=to_email,
            request_id=request_id,
            confirm_url=confirm_url,
            email_dispatch_required=True,
            template="erasure_confirmation_link",
        )
        return True

    async def send_erasure_cooling_off_started(
        self,
        *,
        to_email: str,
        request_id: str,
        scheduled_for_iso: str,
    ) -> bool:
        logger.info(
            "erasure_email_cooling_off_started",
            to_email=to_email,
            request_id=request_id,
            scheduled_for=scheduled_for_iso,
            email_dispatch_required=True,
            template="erasure_cooling_off_started",
        )
        return True

    async def send_erasure_complete(self, *, to_email: str, request_id: str) -> bool:
        logger.info(
            "erasure_email_complete",
            to_email=to_email,
            request_id=request_id,
            email_dispatch_required=True,
            template="erasure_complete",
        )
        return True


_default_email_service: EmailService | None = None


def get_email_service() -> EmailService:
    """Return the singleton EmailService.

    Module-level singleton matches the pattern used for Redis and Qdrant
    clients. Swap the construction here when wiring in a real provider.
    """
    global _default_email_service
    if _default_email_service is None:
        _default_email_service = LoggingEmailService()
    return _default_email_service
