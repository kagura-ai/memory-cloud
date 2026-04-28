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
        ...

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
        ...

    async def send_erasure_complete(self, *, to_email: str, request_id: str) -> bool:
        """Notify that the erasure has completed and the account is gone.

        This must be sent within 24 hours of completion (CLO follow-up #4
        in Issue #360 body).

        Args:
            to_email: Recipient address (still routable — this is the last
                contact we will have with the subject).
            request_id: ``erasure_requests.id`` UUID.
        """
        ...

    async def send_erasure_confirmation(
        self,
        *,
        to_email: str,
        request_id: str,
        confirm_token: str,
        confirm_url: str,
    ) -> bool:
        """Deliver the one-time confirmation token via email (Issue #478).

        Used by the OAuth path of #469 once a real email provider replaces
        ``LoggingEmailService``: the API stops returning ``confirm_token``
        in the response body for OAuth users and instead delivers it via
        this method (email is the canonical second factor for OAuth, just
        as the password re-prompt is for password-auth users).

        Implementations MUST NOT log the raw token or the confirm_url —
        the URL embeds the token as a query parameter, so logging either
        is equivalent to leaking the credential. See ``LoggingEmailService``
        for the redaction discipline; ``ResendEmailService`` ships the
        token to the recipient inbox via TLS-encrypted SMTP and never
        writes it to local logs.

        Args:
            to_email: Recipient address (must match the user's account email).
            request_id: ``erasure_requests.id`` UUID.
            confirm_token: One-time raw confirmation token. **Sensitive** —
                do not log, do not write to disk outside the SMTP envelope.
            confirm_url: Front-end confirmation URL (typically
                ``https://<app-host>/account/erasure/confirm?token=<raw-token>``).
                **Sensitive for the same reason** — contains the raw token.
        """
        ...


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

    async def send_erasure_confirmation(
        self,
        *,
        to_email: str,
        request_id: str,
        confirm_token: str,
        confirm_url: str,
    ) -> bool:
        # Issue #478: confirm_token AND confirm_url are intentionally NOT
        # logged. The URL embeds the token as a query parameter, so logging
        # it is equivalent to logging the token. While LoggingEmailService
        # is the active provider, OAuth users cannot complete the email-
        # only confirmation flow — that flow is gated on a real provider
        # being wired in (#469 + #478 sequencing).
        del confirm_token, confirm_url
        logger.info(
            "erasure_email_confirmation",
            to_email=to_email,
            request_id=request_id,
            email_dispatch_required=True,
            template="erasure_confirmation",
        )
        return True


_default_email_service: EmailService | None = None


def reset_email_service_for_testing() -> None:
    """Drop the singleton so the next ``get_email_service()`` rebuilds it.

    Pytest fixtures use this to switch ``EMAIL_PROVIDER`` between tests
    (e.g. logging vs resend with mocked SDK) without leaking the cached
    instance across the suite. Mirrors the discipline used elsewhere for
    process-wide singletons.
    """
    global _default_email_service
    _default_email_service = None


def get_email_service() -> EmailService:
    """Return the singleton EmailService.

    Module-level singleton matches the pattern used for Redis and Qdrant
    clients. Construction switches on ``settings.email_provider``:
    ``"logging"`` (default) returns the structured-log stub; ``"resend"``
    constructs ``ResendEmailService`` and fails fast at boot if the API
    key is missing.
    """
    global _default_email_service
    if _default_email_service is not None:
        return _default_email_service

    from config.settings import get_settings

    settings = get_settings()
    instance: EmailService
    if settings.email_provider == "resend":
        from services.email_providers.resend import ResendEmailService

        if not settings.resend_api_key:
            raise ValueError("EMAIL_PROVIDER=resend requires RESEND_API_KEY to be set")
        instance = ResendEmailService(
            api_key=settings.resend_api_key,
            from_email=settings.resend_from_email,
        )
    else:
        instance = LoggingEmailService()
    _default_email_service = instance
    return instance
