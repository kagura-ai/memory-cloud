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
        normalized_api_key = (api_key or "").strip()
        if not normalized_api_key:
            raise ValueError("ResendEmailService requires a non-empty api_key")
        normalized_from_email = (from_email or "").strip()
        if not normalized_from_email:
            raise ValueError("ResendEmailService requires a non-empty from_email")
        # The resend SDK uses module-level state — assigning api_key here
        # is process-wide. Idempotent across constructor calls; the last
        # writer wins. Tests that need a clean slate should reset both
        # this attribute and the email_service singleton via
        # services.email_service.reset_email_service_for_testing.
        resend.api_key = normalized_api_key
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
            # Defense in depth: do NOT log ``str(exc)``. The SDK exception
            # message can — under some failure modes — echo request fields,
            # and the request body of ``send_erasure_confirmation`` contains
            # the confirm_url that embeds the raw token. Restrict the log
            # to non-sensitive structured metadata: the exception type and,
            # when the SDK exposes one, an HTTP status code. Operators who
            # need the raw error to debug should reproduce in staging with
            # logging-only or use Resend's own dashboard.
            error_fields: dict[str, Any] = {"error_type": type(exc).__name__}
            status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            if isinstance(status_code, (int, str)):
                error_fields["status_code"] = status_code
            logger.warning(
                f"{log_event}_failed",
                **error_fields,
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
        # ``period`` is one of "daily" / "monthly"; the value comes from
        # ``EmbeddingSpendCapService`` (operator-controlled, not user input)
        # so a-priori we don't need to escape it for the plain-text body,
        # but we still capitalize it for display.
        period_label = period.capitalize()
        if threshold_pct >= 100:
            subject = f"{period_label} embedding spend cap reached — {workspace_name}"
            headline = (
                f"Your workspace has reached its {period} embedding spend cap.\n"
                "New BYOK embedding requests will be rejected until the period rolls.\n"
            )
        else:
            subject = f"{period_label} embedding spend at {threshold_pct}% — {workspace_name}"
            headline = (
                f"Your workspace has used {threshold_pct}% of its {period} embedding "
                f"spend cap.\n"
                "Calls will continue to succeed until 100% is reached.\n"
            )
        text = (
            f"{headline}"
            "\n"
            f"Workspace: {workspace_name}\n"
            f"Workspace ID: {workspace_id}\n"
            f"Period: {period}\n"
            f"Current spend: ${current_usd:.4f}\n"
            f"Cap: ${cap_usd:.4f}\n"
            "\n"
            "Adjust the cap in the workspace admin panel, or wait for the\n"
            "period to roll — daily resets at 00:00 UTC, monthly on the 1st.\n"
        )
        return await self._send(
            to_email=to_email,
            subject=subject,
            text=text,
            log_event="embedding_spend_alert_email",
            log_context={
                "to_email": to_email,
                "workspace_id": workspace_id,
                "period": period,
                "threshold_pct": threshold_pct,
                "template": "embedding_spend_alert",
            },
        )

    async def send_workspace_invitation(
        self,
        *,
        to_email: str,
        inviter_name: str,
        workspace_name: str,
        accept_url: str,
        expires_at_iso: str | None,
    ) -> bool:
        # ``accept_url`` embeds the single-use invitation token as a path
        # segment, so it goes in the body (delivered to the recipient inbox)
        # but is NEVER placed in ``log_context`` — same redaction discipline as
        # send_erasure_confirmation (Issue #654 / #478 Phase-3 precedent).
        expiry_line = (
            f"This invitation expires on {expires_at_iso}.\n"
            if expires_at_iso
            else "This invitation does not expire.\n"
        )
        text = (
            f'{inviter_name} has invited you to join the "{workspace_name}" '
            "workspace on Kagura Memory Cloud.\n"
            "\n"
            "Accept the invitation by opening the link below:\n"
            "\n"
            f"  {accept_url}\n"
            "\n"
            f"{expiry_line}"
            "\n"
            "If you were not expecting this invitation, you can ignore this "
            "email — no action is taken unless you open the link.\n"
        )
        return await self._send(
            to_email=to_email,
            subject=f"You're invited to the {workspace_name} workspace on Kagura",
            text=text,
            log_event="workspace_invitation_email",
            log_context={
                "to_email": to_email,
                "workspace_name": workspace_name,
                "expires_at": expires_at_iso,
                "template": "workspace_invitation",
            },
        )

    async def send_workspace_ownership_transferred(
        self,
        *,
        to_email: str,
        workspace_name: str,
    ) -> bool:
        text = (
            f'You are now the owner of the "{workspace_name}" workspace on '
            "Kagura Memory Cloud.\n"
            "\n"
            "Ownership was transferred to you by the previous owner. You now have "
            "full control of the workspace, including billing and member "
            "management.\n"
            "\n"
            "If you were not expecting this, contact the previous owner or your "
            "Kagura administrator.\n"
        )
        return await self._send(
            to_email=to_email,
            subject=f"You're now the owner of the {workspace_name} workspace on Kagura",
            text=text,
            log_event="workspace_ownership_transferred_email",
            log_context={
                "to_email": to_email,
                "workspace_name": workspace_name,
                "template": "workspace_ownership_transferred",
            },
        )

    async def send_workspace_ownership_force_transferred(
        self,
        *,
        to_email: str,
        workspace_name: str,
    ) -> bool:
        text = (
            f'Ownership of the "{workspace_name}" workspace on Kagura Memory Cloud '
            "was transferred away from your account by a Kagura administrator.\n"
            "\n"
            "Administrators can reassign workspace ownership (for example, when the "
            "owner is unavailable). You no longer have owner privileges on this "
            "workspace.\n"
            "\n"
            "If you believe this was a mistake, contact your Kagura administrator.\n"
        )
        return await self._send(
            to_email=to_email,
            subject=f"Ownership of the {workspace_name} workspace was reassigned",
            text=text,
            log_event="workspace_ownership_force_transferred_email",
            log_context={
                "to_email": to_email,
                "workspace_name": workspace_name,
                "template": "workspace_ownership_force_transferred",
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
