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

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from utils.logger import get_logger

if TYPE_CHECKING:
    from services.security_notification_service import SecurityOccurrence

logger = get_logger(__name__)

# Upper bound callers put on one send (``asyncio.wait_for``): a stuck provider
# must not hold a request, a background task or a job open. Providers keep
# their own transport timeouts below it, so a failure is reported by the
# provider (connect vs. read) before this backstop fires.
EMAIL_SEND_TIMEOUT_SECONDS = 10.0


def redact_recipient(email: str) -> str:
    """Return a short keyed digest of ``email`` for log correlation (#1678).

    Keyed with the audit HMAC key so the digest cannot be reversed with a
    dictionary of addresses; normalized so the same mailbox always maps to the
    same value.

    Args:
        email: The recipient address.

    Returns:
        The first 16 hex characters of HMAC-SHA256(lower(trim(email))).
    """
    from config.settings import get_settings
    from utils.hashing import hmac_sha256_hex

    return hmac_sha256_hex(email.strip().lower(), get_settings().audit_hmac_key)[:16]


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

    async def send_embedding_spend_alert(
        self,
        *,
        to_email: str,
        workspace_id: str,
        workspace_name: str,
        period: str,
        current_usd: float,
        cap_usd: float,
        threshold_pct: int,
    ) -> bool:
        """Notify the workspace owner that an embedding-spend threshold was crossed (#709).

        Args:
            to_email: Workspace owner's email address (resolved by
                ``EmbeddingSpendCapService`` from ``workspace.owner_user_id``).
            workspace_id: Workspace UUID (string form) for cross-reference.
            workspace_name: Display name for the email body.
            period: ``"daily"`` or ``"monthly"`` — selects window vocabulary
                in the message.
            current_usd: BYOK embedding spend so far in the current period.
            cap_usd: The effective cap (tier default or per-workspace override).
            threshold_pct: ``80`` (warning) or ``100`` (cap reached;
                future BYOK calls will be rejected until the period rolls).

        Returns:
            True on delivery (or successful log fallback), False on hard
            failure. Implementations MUST NOT raise.
        """
        ...

    async def send_workspace_invitation(
        self,
        *,
        to_email: str,
        inviter_name: str,
        workspace_name: str,
        accept_url: str,
        expires_at_iso: str | None,
    ) -> bool:
        """Deliver a workspace invitation email (Issue #654).

        Sent as a **courtesy** notification after the invitation row is
        persisted: the row is the source of truth, so an email failure MUST
        NOT roll back the invitation. Like every method here, implementations
        MUST NOT raise — log and return False on failure.

        ``accept_url`` embeds the single-use invitation token as a path
        segment, so it is **sensitive**: implementations MUST NOT log it (the
        token is the credential). Mirror ``send_erasure_confirmation``'s
        redaction discipline — deliver the URL to the recipient inbox, never
        to local logs.

        Args:
            to_email: Invitee's email address.
            inviter_name: Display name of the inviting admin (for the body).
            workspace_name: Workspace display name (for the body / subject).
            accept_url: Absolute invitation accept URL embedding the token.
                **Sensitive** — do not log.
            expires_at_iso: ISO-8601 UTC expiry (Z-suffixed via ``to_utc_iso``)
                or ``None`` when the invitation never expires.

        Returns:
            True on delivery (or logging fallback), False on hard failure.
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
        for the redaction discipline; ``ResendEmailService`` delivers the
        token to the recipient inbox via Resend's HTTPS API and never
        writes it to local logs.

        Args:
            to_email: Recipient address (must match the user's account email).
            request_id: ``erasure_requests.id`` UUID.
            confirm_token: One-time raw confirmation token. **Sensitive** —
                do not log, do not persist it outside the outbound email request.
            confirm_url: Front-end confirmation URL (typically
                ``https://<app-host>/account/erasure/confirm?token=<raw-token>``).
                **Sensitive for the same reason** — contains the raw token.
        """
        ...

    async def send_workspace_ownership_transferred(
        self,
        *,
        to_email: str,
        workspace_name: str,
    ) -> bool:
        """Notify the new owner that a workspace was transferred to them (#1103).

        A **courtesy** notification sent AFTER the transfer has committed (the
        ownership row + audit row are the source of truth), so a delivery failure
        MUST NOT roll anything back. Like every method here, implementations MUST
        NOT raise — log and return False on failure.

        Args:
            to_email: The new owner's account email.
            workspace_name: Workspace display name (for the body / subject).

        Returns:
            True on delivery (or logging fallback), False on hard failure.
        """
        ...

    async def send_workspace_ownership_force_transferred(
        self,
        *,
        to_email: str,
        workspace_name: str,
    ) -> bool:
        """Notify the **displaced previous owner** that an administrator force-
        transferred their workspace ownership (#1101 break-glass).

        A security-transparency notice (the transfer was involuntary, so the
        displaced owner learns of it when they return) — but it follows the same
        best-effort, no-raise contract as every method here: the ownership row +
        audit row are the source of truth, so a delivery failure MUST NOT roll
        anything back.

        Args:
            to_email: The displaced previous owner's account email.
            workspace_name: Workspace display name.

        Returns:
            True on delivery (or logging fallback), False on hard failure.
        """
        ...

    async def send_password_reset(
        self,
        *,
        to_email: str,
        reset_url: str,
        expires_in_minutes: int,
    ) -> bool:
        """Deliver a password-reset link (Issue #1678).

        Sent only for an account with a verified email and a password, in
        answer to ``POST /auth/password/reset-request``. The caller answers
        that request identically whether or not anything was sent.

        ``reset_url`` embeds a single-use token, so it is **sensitive**:
        implementations MUST NOT log it, and SHOULD NOT log the recipient
        address in the clear either (a reset request is a hint about the
        account).

        Args:
            to_email: The account's verified email address.
            reset_url: Front-end reset URL embedding the raw token.
                **Sensitive** — do not log.
            expires_in_minutes: Link lifetime, for the body.

        Returns:
            True on delivery (or logging fallback), False on hard failure.
        """
        ...

    async def send_password_setup(
        self,
        *,
        to_email: str,
        setup_url: str,
        expires_in_minutes: int,
    ) -> bool:
        """Deliver a set-a-password link to a signed-in user (Issue #1678).

        Following the link proves ownership of the address, so a successful
        set also marks the email verified. Same redaction rules as
        ``send_password_reset``.

        Args:
            to_email: The account's email address.
            setup_url: Front-end set-password URL embedding the raw token.
                **Sensitive** — do not log.
            expires_in_minutes: Link lifetime, for the body.

        Returns:
            True on delivery (or logging fallback), False on hard failure.
        """
        ...

    async def send_security_notification(
        self,
        *,
        to_email: str,
        event: str,
        occurrences: list[SecurityOccurrence],
        digest: bool,
        window_minutes: int,
        profile_page_url: str,
        total: int | None = None,
    ) -> bool:
        """Tell the owner about a security-sensitive account change (Issue #1752).

        Sent after the change committed; a delivery failure changes nothing.
        The body (``render_security_notification``) lists each occurrence with
        its UTC time, IP, user agent and the client / key name or acting admin
        where set. It never carries a secret, token, key value or action link.
        Implementations SHOULD NOT log the recipient in the clear nor the
        occurrences (IP and user agent are personal data).

        Args:
            to_email: The owner's deliverable address.
            event: A ``SecurityEvent`` value.
            occurrences: The occurrences to list (sanitized display text).
            digest: True for the trailing email of a coalescing window.
            window_minutes: The coalescing window, for the digest wording.
            profile_page_url: ``<FRONTEND_URL>/profile`` — no token.
            total: Occurrences the digest covers (the buffer keeps only the
                first ones); defaults to ``len(occurrences)``.

        Returns:
            True on delivery (or logging fallback), False on hard failure.

        Raises:
            TimeoutError: The provider may have accepted the email but its
                answer never came (unlike the other methods, which return
                False). The caller counts it as possibly sent and never
                resends it.
        """
        ...

    async def send_capacity_lock_notice(
        self,
        *,
        to_email: str,
        workspace_name: str,
        period_end: datetime,
        over_memories: int,
        over_bytes: int,
        cleanup_url: str,
        contexts_url: str,
    ) -> bool:
        """Warn the owner that the workspace will be locked when it returns to Free (#1941).

        Sent before the paid period ends, when the workspace holds more than
        the Free plan allows. The body names the workspace, the end date, how
        much is over, what is paused versus what keeps working, and links to
        the cleanup and export pages. It never carries memory content.

        Args:
            to_email: The workspace owner's address.
            workspace_name: Display name for the body.
            period_end: When the paid period ends (aware or naive UTC).
            over_memories: Memories over the Free limit (0 when within).
            over_bytes: File bytes over the Free limit (0 when within).
            cleanup_url: The plan / usage page.
            contexts_url: The contexts page (delete by filter, export).

        Returns:
            True on delivery (or logging fallback), False on hard failure.

        Raises:
            TimeoutError: The provider may have accepted the email but its
                answer never came. The caller counts it as possibly sent and
                does not resend (at most once), like
                ``send_security_notification``.
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

    async def send_workspace_invitation(
        self,
        *,
        to_email: str,
        inviter_name: str,
        workspace_name: str,
        accept_url: str,
        expires_at_iso: str | None,
    ) -> bool:
        # accept_url embeds the single-use token; redact it per the Protocol's
        # no-log discipline. inviter_name (a person's name) is also kept out of
        # the log line — the recipient address + workspace are enough for ops
        # triage. The admin who created the invite already has the accept URL
        # from the API response, so logging-mode delivery is not lost.
        del accept_url, inviter_name
        logger.info(
            "workspace_invitation_email",
            to_email=to_email,
            workspace_name=workspace_name,
            expires_at=expires_at_iso,
            email_dispatch_required=True,
            template="workspace_invitation",
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
        # confirm_url embeds the token; redact both per the Protocol's no-log discipline.
        del confirm_token, confirm_url
        logger.info(
            "erasure_email_confirmation",
            to_email=to_email,
            request_id=request_id,
            email_dispatch_required=True,
            template="erasure_confirmation",
        )
        return True

    async def send_embedding_spend_alert(
        self,
        *,
        to_email: str,
        workspace_id: str,
        workspace_name: str,
        period: str,
        current_usd: float,
        cap_usd: float,
        threshold_pct: int,
    ) -> bool:
        logger.info(
            "embedding_spend_alert_email",
            to_email=to_email,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            period=period,
            current_usd=round(current_usd, 4),
            cap_usd=round(cap_usd, 4),
            threshold_pct=threshold_pct,
            email_dispatch_required=True,
            template="embedding_spend_alert",
        )
        return True

    async def send_workspace_ownership_transferred(
        self,
        *,
        to_email: str,
        workspace_name: str,
    ) -> bool:
        logger.info(
            "workspace_ownership_transferred_email",
            to_email=to_email,
            workspace_name=workspace_name,
            email_dispatch_required=True,
            template="workspace_ownership_transferred",
        )
        return True

    async def send_workspace_ownership_force_transferred(
        self,
        *,
        to_email: str,
        workspace_name: str,
    ) -> bool:
        logger.info(
            "workspace_ownership_force_transferred_email",
            to_email=to_email,
            workspace_name=workspace_name,
            email_dispatch_required=True,
            template="workspace_ownership_force_transferred",
        )
        return True

    async def send_password_reset(
        self,
        *,
        to_email: str,
        reset_url: str,
        expires_in_minutes: int,
    ) -> bool:
        # The URL carries the token: never logged. The recipient is logged
        # only as a keyed digest (#1678) — a reset request hints at who has an
        # account, so the address stays out of the log line.
        del reset_url
        logger.info(
            "password_reset_email",
            recipient_hash=redact_recipient(to_email),
            purpose="reset_password",
            expires_in_minutes=expires_in_minutes,
            email_dispatch_required=True,
            template="password_reset",
        )
        return True

    async def send_password_setup(
        self,
        *,
        to_email: str,
        setup_url: str,
        expires_in_minutes: int,
    ) -> bool:
        del setup_url
        logger.info(
            "password_setup_email",
            recipient_hash=redact_recipient(to_email),
            purpose="set_password",
            expires_in_minutes=expires_in_minutes,
            email_dispatch_required=True,
            template="password_setup",
        )
        return True

    async def send_security_notification(
        self,
        *,
        to_email: str,
        event: str,
        occurrences: list[SecurityOccurrence],
        digest: bool,
        window_minutes: int,
        profile_page_url: str,
        total: int | None = None,
    ) -> bool:
        # Event and counts only: the occurrences carry IPs and user agents,
        # and the recipient is logged as a keyed digest (#1752).
        del window_minutes, profile_page_url
        logger.info(
            "security_notification_email",
            recipient_hash=redact_recipient(to_email),
            security_event=event,
            occurrences=total if total is not None else len(occurrences),
            digest=digest,
            email_dispatch_required=True,
            template="security_notification",
        )
        return True

    async def send_capacity_lock_notice(
        self,
        *,
        to_email: str,
        workspace_name: str,
        period_end: datetime,
        over_memories: int,
        over_bytes: int,
        cleanup_url: str,
        contexts_url: str,
    ) -> bool:
        del cleanup_url, contexts_url
        logger.info(
            "capacity_lock_notice_email",
            recipient_hash=redact_recipient(to_email),
            workspace_name=workspace_name,
            period_end=period_end.isoformat(),
            over_memories=over_memories,
            over_bytes=over_bytes,
            email_dispatch_required=True,
            template="capacity_lock_notice",
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
        # Settings._validate_resend_config has already enforced that both
        # resend_api_key and resend_from_email are non-empty when the
        # provider is "resend"; the assert is a type-narrowing aid for
        # pyright (resend_api_key is str | None on the model).
        from services.email_providers.resend import ResendEmailService

        assert settings.resend_api_key is not None
        instance = ResendEmailService(
            api_key=settings.resend_api_key,
            from_email=settings.resend_from_email,
        )
    else:
        instance = LoggingEmailService()
    _default_email_service = instance
    return instance
