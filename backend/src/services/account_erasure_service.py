"""Account erasure orchestrator (Issue #360, GDPR Art.17 / APPI 第22条).

Owns the full lifecycle of an `erasure_requests` row:

    self-service:  pending -> cooling_off -> in_progress -> complete
    admin:                                   in_progress -> complete

Both paths converge on `_execute`, which performs the 12-step cross-store
deletion (Stripe -> Qdrant -> workspace transfer -> Postgres -> Redis ->
audit-log pseudonymize -> finalize) covered by the design pin in #360.

Failure semantics: ``_execute`` is NOT a single atomic transaction.
Each step commits when it completes (workspace ownership transfers,
Postgres deletes, audit pseudonymization, audit-row insert, finalize),
so a failure in step N leaves steps 1..N-1 already persisted. This is
deliberate — partial progress is recorded in ``deleted_data_summary``
and is what ops needs for manual reconciliation. On any raise:

- the in-flight transaction is rolled back (so the failing step's
  partial mutations don't escape),
- the request row is marked ``failed`` with ``failure_reason`` via a
  fresh ``UPDATE`` (independent of the rolled-back ORM state),
- ``deleted_data_summary`` captures whatever steps did complete,
- the exception is re-raised.

Stripe and Qdrant calls are best-effort: their internal try/except
swallows API failures and records the outcome in the summary, so a
Stripe outage never blocks the Postgres delete pipeline (the orphan
Stripe customer can be cleaned up manually via the Stripe dashboard).
"""

from __future__ import annotations

import hmac
import secrets
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.qdrant import delete_user_points
from db.redis import clear_co_activations, clear_user_rate_limits, get_redis_client
from models.auth import (
    APIKey,
    AuditLog,
    ExternalAPIKey,
    OAuth2AuthorizationCode,
    OAuth2Client,
    OAuth2Token,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.erasure import (
    REASON_DETAIL_MAX_CHARS,
    REASON_SELF_SERVICE,
    STATUS_CANCELLED,
    STATUS_COMPLETE,
    STATUS_COOLING_OFF,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    VALID_REASON_CODES,
    ErasureRequest,
)
from services.email_service import EmailService, get_email_service
from services.stripe_service import cancel_subscription_and_delete_customer_for_erasure
from services.system_admin_service import SystemAdminService
from utils.datetime import utcnow
from utils.exceptions import (
    EmailDispatchError,
    ErasureAlreadyInProgressError,
    ErasureForbiddenError,
    ErasureRequestNotFoundError,
    ErasureTokenInvalidError,
    InitialAdminCannotBeErasedError,
    NotFoundException,
    ValidationError,
    WorkspaceTransferRequiredError,
)
from utils.hashing import sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

# 7-day cooling-off period from CLO Gate1 pin #4.
COOLING_OFF_PERIOD = timedelta(days=7)

# Confirmation token TTL in Redis. Aligns with the SLA pin: receipt
# notification within 1 business day, then the user has roughly an hour
# from clicking through the email to confirm. Long enough for a real user
# to act, short enough to limit token-reuse exposure.
CONFIRM_TOKEN_TTL_SECONDS = 3600

# Redis key prefix for the raw confirmation token. The `confirm_token_hash`
# column stores SHA256(token); the raw token only ever lives here.
_CONFIRM_TOKEN_KEY_PREFIX = "erasure_token:"


# Salt used when pseudonymizing audit_log rows for an erased user. A
# stable per-deployment salt (not per-row) lets ops cross-correlate audit
# rows belonging to the same erased user without revealing the email/sub.
# Goal is irreversibility, not rainbow-table protection — once the user
# row is gone there is no plaintext to recover.
def _audit_salt() -> str:
    return get_settings().audit_pseudo_salt


_sha256_hex = sha256_hex  # backward-compat alias for tests + this module


class AccountErasureService:
    """Service-layer orchestrator for GDPR right-to-erasure.

    Constructed per-request with an AsyncSession (matches the codebase
    convention used by MemoryService, WorkspaceService, etc.). The
    EmailService is injected so tests can swap in a fake; production
    code uses the module-level `get_email_service()` singleton.
    """

    def __init__(self, db: AsyncSession, email_service: EmailService | None = None):
        self.db = db
        self.email_service = email_service or get_email_service()

    # ------------------------------------------------------------------
    # Self-service path
    # ------------------------------------------------------------------

    async def request_self_service_erasure(
        self,
        *,
        user_id: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[ErasureRequest, str | None]:
        """Create a pending self-service erasure request.

        Returns the row plus the **response token**. Issue #469: the
        response token is the raw token for password-auth users (the
        response body is their delivery channel — they re-enter their
        password as the second factor at confirm time) and ``None`` for
        OAuth users (the raw token is delivered out-of-band via
        ``send_erasure_confirmation`` email — keeping it out of the
        response body removes a redundant copy that would otherwise
        widen the disclosure surface).

        The raw token always exists internally regardless of channel:
        SHA256 lands in Postgres (``confirm_token_hash``) and the raw
        value lands in Redis under ``erasure_token:{token}`` with TTL
        1h. Both ``confirm_self_service`` paths (password + OAuth) read
        from the same Redis key.

        Raises:
            NotFoundException: User does not exist.
            InitialAdminCannotBeErasedError: User is the protected initial admin.
            ErasureAlreadyInProgressError: An active (pending/cooling_off) request
                already exists for this user.
            EmailDispatchError: OAuth user but ``send_erasure_confirmation``
                failed; the pending row has been rolled back so the user is
                free to retry. Maps to HTTP 503.
        """
        target = await self._load_user_or_404(user_id)
        if target.is_initial_admin:
            raise InitialAdminCannotBeErasedError()

        existing = await self._find_active_request(user_id)
        if existing:
            raise ErasureAlreadyInProgressError(existing.status)

        token = secrets.token_urlsafe(32)
        token_hash = _sha256_hex(token)

        request = ErasureRequest(
            user_id=user_id,
            user_email_hash=_sha256_hex(target.email),
            initiated_by=user_id,
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            reason_detail=None,
            status=STATUS_PENDING,
            confirm_token_hash=token_hash,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.db.add(request)
        # Order matters: flush() (issues INSERT, populates request.id, surfaces
        # IntegrityError early) → Redis SETEX → OAuth confirmation email (if
        # any) → commit() → receipt email (post-commit, fire-and-forget).
        #
        # Failure modes:
        #  - flush() IntegrityError → rollback (no row, no Redis key) → user
        #    sees ErasureAlreadyInProgressError, can retry once the prior
        #    request is resolved.
        #  - Redis SETEX raises → rollback PG → no orphan row.
        #  - send_erasure_confirmation fails (OAuth only, Issue #469) →
        #    rollback PG + best-effort Redis key delete → no orphan row,
        #    no orphan token. Pre-commit placement is essential because
        #    OAuth users have no in-band token: a committed pending row
        #    without delivered email would wedge the user (cancel works
        #    on pending, but only if they realize there is a pending row
        #    to cancel — UX failure). Explicit Redis delete narrows the
        #    orphan-token window from the 1h TTL to "immediate"; this
        #    matters when a provider partially delivers (recipient inbox
        #    receives the link) before raising on the response, where
        #    the 1h orphan would leave a live confirm path against a
        #    rolled-back row.
        #  - commit() fails after Redis SETEX (and after OAuth email send) →
        #    orphan Redis key + email already sent. Rare; Redis TTL bounds
        #    the orphan and the receipt email is what the user sees.
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ErasureAlreadyInProgressError("active") from exc

        redis = get_redis_client()
        try:
            await redis.setex(
                f"{_CONFIRM_TOKEN_KEY_PREFIX}{token}",
                CONFIRM_TOKEN_TTL_SECONDS,
                str(request.id),
            )
        except Exception:
            await self.db.rollback()
            raise

        is_oauth = target.auth_method == "oauth"

        if is_oauth:
            base_url = get_settings().frontend_url.rstrip("/")
            confirm_url = f"{base_url}/account/erasure/confirm?token={token}"
            try:
                sent = await self.email_service.send_erasure_confirmation(
                    to_email=target.email,
                    request_id=str(request.id),
                    confirm_token=token,
                    confirm_url=confirm_url,
                )
            except Exception as exc:
                # Protocol contract says implementations MUST NOT raise on
                # send failure, but defend against future regressions and
                # misbehaving custom backends. Log only the type and a
                # numeric status_code if the SDK exception exposes one —
                # str(exc) can echo the SDK request body which contains
                # confirm_url (and thus the raw token). Mirror the existing
                # discipline in email_providers/resend.py:_send so future
                # SDK integrations have a single referenced pattern.
                log_kwargs: dict[str, Any] = {
                    "request_id": str(request.id),
                    "user_id": user_id,
                    "error_type": type(exc).__name__,
                }
                exc_status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                if isinstance(exc_status, int):
                    log_kwargs["status_code"] = exc_status
                logger.error("erasure_confirmation_email_dispatch_failed", **log_kwargs)
                await self.db.rollback()
                # Best-effort Redis cleanup: explicitly delete the token
                # key so a partially-delivered email cannot drive a
                # confirm against the now-rolled-back row. Failure here
                # falls back to the 1h TTL self-clean (no raise — the
                # primary error is the email dispatch, not Redis).
                try:
                    await redis.delete(f"{_CONFIRM_TOKEN_KEY_PREFIX}{token}")
                except Exception:
                    pass
                # ``from None`` suppresses the original __context__ — SDK
                # exception messages can leak the token via repr/str.
                # ``EmailDispatchError`` is zero-argument by design (see
                # its docstring) so no token-bearing string can land in
                # the exception's surfaced ``message`` field; the cause
                # is captured via the structured log entry above.
                raise EmailDispatchError() from None
            if not sent:
                logger.error(
                    "erasure_confirmation_email_dispatch_failed",
                    request_id=str(request.id),
                    user_id=user_id,
                    error_type="send_returned_false",
                )
                await self.db.rollback()
                try:
                    await redis.delete(f"{_CONFIRM_TOKEN_KEY_PREFIX}{token}")
                except Exception:
                    pass
                raise EmailDispatchError()

        await self.db.commit()
        await self.db.refresh(request)

        # Receipt is advisory and fires post-commit (matches the pattern
        # used by send_erasure_cooling_off_started and send_erasure_complete).
        # A receipt failure is acceptable: the row exists, the user already
        # has the token (password) or the confirmation email (OAuth).
        await self.email_service.send_erasure_receipt(
            to_email=target.email,
            request_id=str(request.id),
        )

        logger.info(
            "erasure_request_created",
            request_id=str(request.id),
            user_id=user_id,
            auth_method=target.auth_method,
            token_in_response=not is_oauth,
        )
        return request, (None if is_oauth else token)

    async def confirm_self_service(
        self,
        *,
        user_id: str,
        token: str,
        password: str | None = None,
    ) -> ErasureRequest:
        """Verify the confirmation token (and password for password users)
        and move pending -> cooling_off.

        Raises:
            ErasureTokenInvalidError: Token missing/expired/mismatched.
            ErasureForbiddenError: Password mismatch on the password path.
            ErasureRequestNotFoundError: No pending request exists.
        """
        target = await self._load_user_or_404(user_id)

        # Resolve and validate token via Redis (raw token never on disk).
        redis = get_redis_client()
        redis_key = f"{_CONFIRM_TOKEN_KEY_PREFIX}{token}"
        request_id_str = await redis.get(redis_key)
        if not request_id_str:
            raise ErasureTokenInvalidError()

        try:
            request_id = UUID(request_id_str)
        except (ValueError, TypeError) as exc:
            raise ErasureTokenInvalidError() from exc

        request = await self._load_request_or_404(request_id)
        if request.user_id != user_id:
            # Token from a different user's request — refuse without leaking
            # which other user is involved.
            raise ErasureTokenInvalidError()

        if request.status != STATUS_PENDING:
            raise ErasureTokenInvalidError(
                f"Request is not pending (current status: {request.status})"
            )

        # Defense-in-depth: also compare against stored hash. Catches any
        # Redis/Postgres divergence (e.g. token was rotated server-side
        # between issue and confirm). Constant-time compare so a malicious
        # client cannot infer the stored hash one byte at a time.
        if not hmac.compare_digest(request.confirm_token_hash or "", _sha256_hex(token)):
            raise ErasureTokenInvalidError()

        # Password re-confirm for password users (Q5 design). OAuth users
        # rely on the email-link click as the second factor; the active
        # session cookie was the first.
        if target.auth_method == "password":
            if not password:
                raise ErasureForbiddenError("Password required to confirm erasure")
            from auth.password import verify_password

            if not target.password_hash or not verify_password(password, target.password_hash):
                raise ErasureForbiddenError("Incorrect password")

        # Pre-check: surface WorkspaceTransferRequiredError at confirm time
        # rather than 7 days later in the cron sweep. Without this the
        # user's only signal would be reading status=failed via GET
        # /me/account/erasure-request a week after they confirmed —
        # actively bad UX. Workspace state can still change during
        # cooling-off (admin leaves, etc.), so this is best-effort, not
        # a guarantee. The cron's identical check remains the safety net.
        await self._check_no_blocking_workspace_transfers(user_id)

        now = utcnow()
        request.status = STATUS_COOLING_OFF
        request.confirmed_at = now
        request.scheduled_for = now + COOLING_OFF_PERIOD
        await self.db.commit()
        await self.db.refresh(request)

        # Burn the token so a leaked link can't be replayed.
        await redis.delete(redis_key)

        await self.email_service.send_erasure_cooling_off_started(
            to_email=target.email,
            request_id=str(request.id),
            scheduled_for_iso=request.scheduled_for.isoformat(),
        )

        logger.info(
            "erasure_request_confirmed",
            request_id=str(request.id),
            user_id=user_id,
            scheduled_for=request.scheduled_for.isoformat(),
        )
        return request

    async def cancel_self_service(self, *, user_id: str) -> ErasureRequest:
        """Cancel a pending or cooling_off request.

        Both states are cancellable:
        - ``pending``: user requested but never confirmed. The Redis token
          may have already expired (1h TTL); without this allow-cancel, a
          forgotten pending row would block all future erasure requests
          from this user (partial unique index treats pending as active).
        - ``cooling_off``: user confirmed but the 7-day window hasn't elapsed.

        ``in_progress`` rows are NOT cancellable — the orchestrator is
        already running, and racing it would risk an inconsistent half-
        applied erasure.

        Raises:
            ErasureRequestNotFoundError: No active cancellable request.
        """
        request = await self._find_active_request(user_id)
        if request is None or request.status not in (
            STATUS_PENDING,
            STATUS_COOLING_OFF,
        ):
            raise ErasureRequestNotFoundError(user_id)

        cancelled_from = request.status
        request.status = STATUS_CANCELLED
        request.cancelled_at = utcnow()
        await self.db.commit()
        await self.db.refresh(request)

        logger.info(
            "erasure_request_cancelled",
            request_id=str(request.id),
            user_id=user_id,
            cancelled_from=cancelled_from,
        )
        return request

    # ------------------------------------------------------------------
    # Admin force-erase path
    # ------------------------------------------------------------------

    async def admin_force_erase(
        self,
        *,
        target_user_id: str,
        initiator_user_id: str,
        reason_code: str,
        reason_detail: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> ErasureRequest:
        """Create an admin-initiated request and execute it inline.

        Skips the cooling-off period — admin actions are deliberate and
        usually triggered by a legal/abuse signal that wants immediate
        effect. The reason_code/reason_detail pair is mandatory and
        constrained by the CHECK constraint on the table (admin-only
        codes, not `self_service`).

        Raises:
            ValidationError: Bad reason_code/reason_detail.
            NotFoundException / InitialAdminCannotBeErasedError /
            ErasureAlreadyInProgressError / WorkspaceTransferRequiredError:
            All forwarded from the inline execution path.
        """
        if reason_code == REASON_SELF_SERVICE:
            raise ValidationError("reason_code 'self_service' is not allowed for admin path")
        if reason_code not in VALID_REASON_CODES:
            raise ValidationError(f"Invalid reason_code: {reason_code}", field="reason_code")
        if reason_detail and len(reason_detail) > REASON_DETAIL_MAX_CHARS:
            raise ValidationError(
                f"reason_detail exceeds {REASON_DETAIL_MAX_CHARS} chars",
                field="reason_detail",
            )

        target = await self._load_user_or_404(target_user_id)
        if target.is_initial_admin:
            raise InitialAdminCannotBeErasedError()

        # Cannot delete the last remaining admin (mirrors SystemAdminService).
        admin_service = SystemAdminService(self.db)
        can_delete, reason = await admin_service.can_delete_admin(target_user_id)
        if not can_delete:
            raise ErasureForbiddenError(reason)

        existing = await self._find_active_request(target_user_id)
        if existing:
            raise ErasureAlreadyInProgressError(existing.status)

        now = utcnow()
        request = ErasureRequest(
            user_id=target_user_id,
            user_email_hash=_sha256_hex(target.email),
            initiated_by=initiator_user_id,
            is_self_service=False,
            reason_code=reason_code,
            reason_detail=reason_detail,
            status=STATUS_IN_PROGRESS,
            requested_at=now,
            confirmed_at=now,
            started_at=now,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.db.add(request)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            # Same race protection as the self-service path: partial unique
            # index rejects concurrent admin force-erase + sweep collisions.
            await self.db.rollback()
            raise ErasureAlreadyInProgressError("active") from exc
        await self.db.refresh(request)

        logger.info(
            "erasure_admin_force_started",
            request_id=str(request.id),
            target_user_id=target_user_id,
            initiator_user_id=initiator_user_id,
            reason_code=reason_code,
        )

        await self._execute(request, target)
        return request

    # ------------------------------------------------------------------
    # Cron sweep entry point
    # ------------------------------------------------------------------

    async def sweep_pending_erasures(self, *, batch_size: int = 50) -> int:
        """Pick up cooling_off rows whose scheduled_for has passed and execute.

        Also handles two stale-state cleanup paths:

        - **Stale ``in_progress``** (``started_at`` older than 1h): these
          are runs that were marked in-progress but did not complete
          (process SIGTERM/OOM between commit and ``_execute``). Picked
          up by the main sweep query and re-executed.

        - **Stale ``pending``** (``requested_at`` older than 1h + 5min
          grace): the Redis confirm token (TTL 1h) has expired and the
          user can no longer confirm. The row would otherwise block all
          future erasure requests via the partial unique index. Marked
          ``cancelled`` in a single UPDATE before the main sweep — no
          per-row execution needed since these rows have nothing to
          delete (they never reached cooling_off).

        Uses ``FOR UPDATE SKIP LOCKED`` so multiple scheduler workers (if
        we ever scale horizontally) won't double-execute the same row.

        Returns:
            Number of requests executed (does NOT count cancelled stale
            pending rows — those are reclaim, not execution).
        """
        from sqlalchemy import or_

        now = utcnow()
        # 1 hour is well over a normal _execute (seconds-to-minutes for the
        # cross-store deletes); a row in_progress this long is presumed
        # crashed.
        stale_in_progress_cutoff = now - timedelta(hours=1)

        # Pending token TTL is 1h (CONFIRM_TOKEN_TTL_SECONDS); add 5min
        # grace so we don't race a user who just clicked confirm but
        # whose Redis SETEX is in flight.
        pending_token_expired_cutoff = now - timedelta(hours=1, minutes=5)
        stale_pending_result = await self.db.execute(
            update(ErasureRequest)
            .where(
                (ErasureRequest.status == STATUS_PENDING)
                & (ErasureRequest.requested_at <= pending_token_expired_cutoff)
            )
            .values(status=STATUS_CANCELLED, cancelled_at=now)
        )
        if (stale_pending_result.rowcount or 0) > 0:
            logger.info(
                "erasure_sweep_pending_reclaimed",
                count=stale_pending_result.rowcount,
                reason="token_expired_no_confirm",
            )
            await self.db.commit()
        result = await self.db.execute(
            select(ErasureRequest)
            .where(
                or_(
                    (ErasureRequest.status == STATUS_COOLING_OFF)
                    & (ErasureRequest.scheduled_for <= now),
                    (ErasureRequest.status == STATUS_IN_PROGRESS)
                    & (ErasureRequest.started_at <= stale_in_progress_cutoff),
                )
            )
            .order_by(ErasureRequest.scheduled_for.asc().nulls_last())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        due_requests = list(result.scalars().all())
        if not due_requests:
            return 0

        # Mark all as in_progress immediately (under the same FOR UPDATE
        # lock) so a second sweeper waking up between iterations cannot
        # also see them as cooling_off.
        for req in due_requests:
            req.status = STATUS_IN_PROGRESS
            req.started_at = now
        await self.db.commit()

        # Bulk-load every target user up-front rather than one SELECT per
        # request — collapses up to `batch_size` round-trips into one.
        user_ids = [req.user_id for req in due_requests]
        users_result = await self.db.execute(select(User).where(User.user_id.in_(user_ids)))
        users_by_id: dict[str, User] = {u.user_id: u for u in users_result.scalars().all()}

        executed = 0
        for req in due_requests:
            target = users_by_id.get(req.user_id)
            if target is None:
                # User row already gone (race / manual cleanup). Mark complete
                # with empty summary; nothing to delete.
                req.status = STATUS_COMPLETE
                req.completed_at = utcnow()
                req.deleted_data_summary = {"note": "user_row_already_absent"}
                await self.db.commit()
                continue
            try:
                await self._execute(req, target)
                executed += 1
            except Exception as exc:
                logger.error(
                    "erasure_sweep_execute_failed",
                    request_id=str(req.id),
                    user_id=req.user_id,
                    error=str(exc),
                    exc_info=True,
                )
                # `_execute` already marked status=failed; continue to
                # the next request in this batch.
                continue
        return executed

    # ------------------------------------------------------------------
    # Core execution (12-step orchestrator)
    # ------------------------------------------------------------------

    async def _execute(self, request: ErasureRequest, target: User) -> None:
        """Run the cross-store erasure for a single request.

        Order: Stripe -> Qdrant -> workspaces -> Postgres -> Redis ->
        audit_logs pseudonymize -> audit row -> finalize. Stripe and
        Qdrant first so a failure leaves a recoverable Postgres state;
        Redis last because session/rate-limit data is ephemeral and
        re-creatable from cookies if anything goes wrong.
        """
        summary: dict[str, Any] = {}
        try:
            owned_workspaces = await self._list_owned_workspaces(target.user_id)

            # Step 1: Stripe (best-effort)
            stripe_summary: dict[str, Any] = {"workspaces_processed": []}
            for ws in owned_workspaces:
                ws_result = await cancel_subscription_and_delete_customer_for_erasure(ws)
                if ws_result["subscription_cancelled"] or ws_result["customer_deleted"]:
                    stripe_summary["workspaces_processed"].append(
                        {"workspace_id": str(ws.id), **ws_result}
                    )
            summary["stripe"] = stripe_summary

            # Step 2: Qdrant (raises on failure -> caught below)
            summary["qdrant"] = await delete_user_points(target.user_id)

            # Step 3: workspace ownership transfer / abort gate
            summary["workspaces"] = await self._handle_owned_workspaces(
                target.user_id, owned_workspaces
            )

            # Step 4: Postgres deletes (FK-safe order)
            summary["postgres"] = await self._delete_postgres(target)

            # Step 5: pseudonymize existing audit_logs referencing this user
            summary["audit_logs_pseudonymized"] = await self._pseudonymize_audit_logs(
                target.user_id, target.email
            )

            # Step 6: Redis cleanup (best-effort)
            summary["redis"] = await self._clear_redis(target.user_id)

            # Step 7: write the new "account_erasure" audit row
            await self._write_audit_log(request, target, summary)

            # Step 8: finalize the erasure_requests row
            await self._finalize(request, summary)

            # Step 9: completion notification — outside the success path's
            # transactional integrity guarantees. A future real EmailService
            # could raise on transient SMTP failure; that must NOT cause
            # the just-committed `complete` row to be overwritten as
            # `failed`. Swallow + log instead.
            try:
                await self.email_service.send_erasure_complete(
                    to_email=target.email,
                    request_id=str(request.id),
                )
            except Exception as exc:
                logger.error(
                    "erasure_complete_email_failed",
                    request_id=str(request.id),
                    error=str(exc),
                )

        except WorkspaceTransferRequiredError as exc:
            # Caller-actionable error. Move the row to the terminal `failed`
            # state with a structured reason so the operator can fix
            # workspace roles and create a *new* request, rather than
            # reusing this one.
            #
            # Earlier revisions reverted the row to `cooling_off`/`pending`,
            # but that produced two pathological behaviours flagged by the
            # Copilot review on PR #464:
            #   - Self-service: `scheduled_for` was already in the past, so
            #     the cron sweep picked it up again immediately and fired
            #     the same error every hour until the user cancelled.
            #   - Admin: `_find_active_request` treats `pending` as active,
            #     so a retry call to `admin_force_erase` hit
            #     `ErasureAlreadyInProgressError` — the admin couldn't
            #     unstick the row without manual DB intervention.
            #
            # Terminal `failed` is the cleanest semantics: the API exception
            # tells the caller exactly what to fix (HTTP 409 + workspace_id
            # + member_count), and a fresh request can be created once the
            # workspace state is corrected.
            await self.db.rollback()
            await self.db.execute(
                update(ErasureRequest)
                .where(ErasureRequest.id == request.id)
                .values(
                    status=STATUS_FAILED,
                    failure_reason=(
                        f"workspace_transfer_required: workspace_id={exc.details.get('workspace_id')} "
                        f"member_count={exc.details.get('member_count')}. "
                        "Promote another member to admin (or remove members), then create a new erasure request."
                    )[:1000],
                    deleted_data_summary=summary or None,
                )
            )
            await self.db.commit()
            raise
        except Exception as exc:
            logger.error(
                "erasure_execute_failed",
                request_id=str(request.id),
                user_id=target.user_id,
                error=str(exc),
                exc_info=True,
            )
            await self.db.rollback()
            # Use a fresh write to record the failure without depending on
            # the just-rolled-back ORM state.
            await self.db.execute(
                update(ErasureRequest)
                .where(ErasureRequest.id == request.id)
                .values(
                    status=STATUS_FAILED,
                    failure_reason=str(exc)[:1000],
                    deleted_data_summary=summary or None,
                )
            )
            await self.db.commit()
            raise

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    async def _list_owned_workspaces(self, user_id: str) -> list[Workspace]:
        """All workspaces this user owns (drives Stripe + member checks)."""
        result = await self.db.execute(select(Workspace).where(Workspace.owner_user_id == user_id))
        return list(result.scalars().all())

    async def _check_no_blocking_workspace_transfers(self, user_id: str) -> None:
        """Raise WorkspaceTransferRequiredError if any owned workspace would
        block the eventual sweep execution.

        Identical predicate to ``_handle_owned_workspaces``: a workspace
        with members but no alternate admin/owner is "blocking". Sole-owner
        workspaces (no other members) are fine — they get deleted in step 4.

        Pre-check is best-effort: workspace membership can change during
        the 7-day cooling-off window, so the cron's identical check is the
        actual enforcement point. This method just surfaces the issue at
        confirm time so the user gets a 409 instead of waiting a week to
        learn their request will fail.
        """
        owned = await self._list_owned_workspaces(user_id)
        if not owned:
            return

        ws_ids = [ws.id for ws in owned]
        members_result = await self.db.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(ws_ids))
        )
        members_by_ws: dict[Any, list[WorkspaceMember]] = {}
        for m in members_result.scalars().all():
            members_by_ws.setdefault(m.workspace_id, []).append(m)

        for ws in owned:
            other_members = [m for m in members_by_ws.get(ws.id, []) if m.user_id != user_id]
            if not other_members:
                continue  # sole owner — workspace will be deleted in step 4
            other_admins = [m for m in other_members if m.role in ("owner", "admin")]
            if not other_admins:
                raise WorkspaceTransferRequiredError(
                    workspace_id=str(ws.id), member_count=len(other_members)
                )

    async def _handle_owned_workspaces(
        self, user_id: str, workspaces: list[Workspace]
    ) -> dict[str, Any]:
        """Transfer ownership where possible, refuse where not.

        Per Q4 design:
            - Other admin in the workspace -> auto-transfer. The new owner
              is the lowest-``user_id`` admin/owner in the workspace
              (lexicographic ASC). ``WorkspaceMember`` carries no email
              column, so user_id is the deterministic-ordering criterion.
            - Member(s) but no other admin -> raise WorkspaceTransferRequiredError.
            - Sole owner -> no transfer; workspace will be deleted in step 4.
        """
        transfers: list[dict[str, str]] = []
        sole_owner_count = 0

        if not workspaces:
            return {"transferred": transfers, "sole_owner_workspaces": sole_owner_count}

        # Bulk-load every member row for these workspaces in one round-trip
        # and group in memory — the per-workspace SELECT pattern was an N+1
        # for users with multiple owned workspaces.
        ws_ids = [ws.id for ws in workspaces]
        members_result = await self.db.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(ws_ids))
        )
        members_by_ws: dict[Any, list[WorkspaceMember]] = {}
        for m in members_result.scalars().all():
            members_by_ws.setdefault(m.workspace_id, []).append(m)

        for ws in workspaces:
            other_members = [m for m in members_by_ws.get(ws.id, []) if m.user_id != user_id]
            if not other_members:
                sole_owner_count += 1
                continue

            other_admins = sorted(
                (m for m in other_members if m.role in ("owner", "admin")),
                key=lambda m: m.user_id or "",
            )
            if not other_admins:
                raise WorkspaceTransferRequiredError(
                    workspace_id=str(ws.id), member_count=len(other_members)
                )

            new_owner = other_admins[0]
            ws.owner_user_id = new_owner.user_id
            new_owner.role = "owner"
            transfers.append({"workspace_id": str(ws.id), "new_owner_user_id": new_owner.user_id})

        # Commit so the cascade on workspace delete (step 4) sees the new
        # owner_user_id rather than the about-to-be-deleted user_id.
        if transfers:
            await self.db.commit()

        return {"transferred": transfers, "sole_owner_workspaces": sole_owner_count}

    async def _delete_postgres(self, target: User) -> dict[str, int]:
        """Delete every Postgres row that references the user.

        Order matters because not every column is FK-cascaded — most
        cross-table user references are plain VARCHAR (OAuth2 sub) with
        no cascade. The full sweep here is the application-layer
        equivalent of `ON DELETE CASCADE` for all per-user data.
        """
        # Optional model imports so the service doesn't crash if a model
        # is renamed/removed in a future refactor.
        from models.auth import PlanChange, UsageStats, UserPlan
        from models.memory import GraphMemory

        user_id = target.user_id
        counts: dict[str, int] = {}

        # OAuth2 tokens / authorization codes / clients first — no FK
        # cascade from users to these.
        counts["oauth_tokens"] = await self._count_and_delete(
            OAuth2Token, OAuth2Token.user_id == user_id
        )
        counts["oauth_authorization_codes"] = await self._count_and_delete(
            OAuth2AuthorizationCode, OAuth2AuthorizationCode.user_id == user_id
        )
        counts["oauth_clients"] = await self._count_and_delete(
            OAuth2Client, OAuth2Client.owner_id == user_id
        )

        # Direct per-user tables.
        counts["external_api_keys"] = await self._count_and_delete(
            ExternalAPIKey, ExternalAPIKey.user_id == user_id
        )
        counts["api_keys"] = await self._count_and_delete(APIKey, APIKey.user_id == user_id)
        counts["graph_memory"] = await self._count_and_delete(
            GraphMemory, GraphMemory.user_id == user_id
        )
        counts["usage_stats"] = await self._count_and_delete(
            UsageStats, UsageStats.user_id == user_id
        )
        counts["user_plans"] = await self._count_and_delete(UserPlan, UserPlan.user_id == user_id)

        # Workspace memberships and invitations.
        counts["workspace_members"] = await self._count_and_delete(
            WorkspaceMember, WorkspaceMember.user_id == user_id
        )
        counts["workspace_invitations"] = await self._count_and_delete(
            WorkspaceInvitation,
            (WorkspaceInvitation.invited_by == user_id)
            | (WorkspaceInvitation.email == target.email),
        )

        # Pseudonymize plan_changes.changed_by — keep the audit trail but
        # break the link to the deleted user (legal retention concern).
        counts["plan_changes_pseudonymized"] = await self._pseudonymize_field(
            PlanChange, PlanChange.changed_by, user_id
        )

        # Workspaces owned by the user. After step 3 ran, only sole-owner
        # workspaces remain pointing here. The cascade chain wipes
        # contexts/memories/edges/etc.
        counts["workspaces"] = await self._count_and_delete(
            Workspace, Workspace.owner_user_id == user_id
        )

        # Finally the user row itself.
        await self.db.delete(target)
        await self.db.commit()
        counts["users"] = 1

        return counts

    async def _count_and_delete(self, model: Any, where_clause: Any) -> int:
        """Delete rows and return the affected count from the cursor.

        Single round-trip: PostgreSQL returns ``rowcount`` from ``DELETE``
        directly, so we don't need a separate ``SELECT count()``.
        """
        result = await self.db.execute(delete(model).where(where_clause))
        return result.rowcount or 0

    async def _pseudonymize_field(self, model: Any, column: Any, user_id: str) -> int:
        """SHA256-pseudonymize a column across all matching rows.

        Used for legal-retention tables (e.g. plan_changes) where the row
        must survive but the personal-data link to the deleted user must
        not.
        """
        pseudonym = sha256_hex(user_id, salt=_audit_salt())
        result = await self.db.execute(
            update(model).where(column == user_id).values({column: pseudonym})
        )
        return result.rowcount or 0

    async def _pseudonymize_audit_logs(self, user_id: str, email: str) -> int:
        """Replace user_id/resource (and conditionally user_email) on audit_logs.

        Audit rows are kept (legal retention) but the link to the deleted
        user is broken. The pseudonyms use a per-deployment salt so
        cross-row correlation for the SAME erased user is still possible
        for compliance investigations, but the original sub/email is
        unrecoverable.

        Column-by-column rules:
        - ``user_id``: ALWAYS rewritten when the row matches the erased
          subject's user_id. ``user_id`` is the subject column.
        - ``user_email``: in this codebase, ``audit_logs.user_email`` is
          often the *actor's* email, not the subject's (see
          RoleManager.assign_role and SystemAdminService.promote/demote).
          A blind overwrite would clobber the actor's identity and
          misattribute every audit row about the subject. Use a SQL
          CASE: rewrite only when ``user_email`` equals the erased
          subject's email; preserve otherwise.
        - ``resource``: conventionally ``user:{email}`` for user-targeted
          events. Chain ``func.replace`` to swap any occurrence of the
          subject's email or raw user_id with their pseudonyms — covers
          ``user:{email}`` and any future ``user_id``-bearing shape
          without enumerating every event flavour.

        Caught by Copilot /review iter 2: prior version overwrote
        user_email indiscriminately, breaking actor attribution.
        """
        from sqlalchemy import case, func

        salt = _audit_salt()
        user_pseudonym = sha256_hex(user_id, salt=salt)
        email_pseudonym = sha256_hex(email, salt=salt)
        # Replace email first (longer / more specific), then user_id, so a
        # row whose resource contains both is fully scrubbed.
        scrubbed_resource = func.replace(
            func.replace(AuditLog.resource, email, email_pseudonym),
            user_id,
            user_pseudonym,
        )
        # Pseudonymize user_email only when it equals the erased subject's
        # email — preserves actor email on audit rows where actor != subject.
        conditional_email = case(
            (AuditLog.user_email == email, email_pseudonym),
            else_=AuditLog.user_email,
        )
        result = await self.db.execute(
            update(AuditLog)
            .where(AuditLog.user_id == user_id)
            .values(
                user_id=user_pseudonym,
                user_email=conditional_email,
                resource=scrubbed_resource,
            )
        )
        return result.rowcount or 0

    async def _clear_redis(self, user_id: str) -> dict[str, int]:
        """Best-effort Redis cleanup. Failures are logged inside helpers."""
        # SessionManager uses a sync Redis client — fetch the live instance
        # via the auth module's public accessor so we go through the same setup.
        from api.routes.auth import get_session_manager

        sessions_deleted = 0
        session_manager = get_session_manager()
        if session_manager is not None:
            sessions_deleted = session_manager.delete_user_sessions(user_id)

        return {
            "sessions": sessions_deleted,
            "co_act": await clear_co_activations(user_id),
            "rate_limit": await clear_user_rate_limits(user_id),
        }

    async def _write_audit_log(
        self, request: ErasureRequest, target: User, summary: dict[str, Any]
    ) -> None:
        """Append the canonical `account_erasure` audit row, fully pseudonymized.

        This row is born AFTER the bulk pseudonymize step (Step 5) ran, so
        we cannot rely on a later sweep to scrub it. Inline pseudonymization
        guarantees no plaintext email or user_id ever lands in audit_logs
        for this event — required for GDPR Art.5(1)(c) compliance.
        """
        salt = _audit_salt()
        user_pseudonym = _sha256_hex(target.user_id, salt=salt)
        email_pseudonym = _sha256_hex(target.email, salt=salt)
        # Self-service `initiated_by` IS the subject's raw user_id (the
        # user clicked their own delete button). Storing it verbatim in
        # audit_logs.user_metadata would re-introduce a stable plaintext
        # identifier for the erased user — a regression of the
        # pseudonymization invariant the rest of this row enforces.
        # Admin path `initiated_by` is the admin's user_id (NOT the
        # erased subject), which is legitimate audit-trail information
        # and stays plaintext.
        initiated_by_value = user_pseudonym if request.is_self_service else request.initiated_by
        self.db.add(
            AuditLog(
                user_email=email_pseudonym,
                user_id=user_pseudonym,
                action="account_erasure",
                resource=f"user_pseudonym:{user_pseudonym[:16]}",
                user_metadata={
                    "request_id": str(request.id),
                    "is_self_service": request.is_self_service,
                    "initiated_by": initiated_by_value,
                    "reason_code": request.reason_code,
                    "deleted_data_summary": summary,
                },
                ip_address=request.ip_address,
                user_agent=request.user_agent,
            )
        )
        await self.db.commit()

    async def _finalize(self, request: ErasureRequest, summary: dict[str, Any]) -> None:
        """Mark the erasure_requests row complete.

        Done with an UPDATE rather than ORM mutation because the request
        object's session may have been touched by intermediate commits.
        """
        await self.db.execute(
            update(ErasureRequest)
            .where(ErasureRequest.id == request.id)
            .values(
                status=STATUS_COMPLETE,
                completed_at=utcnow(),
                deleted_data_summary=summary,
            )
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    async def _load_user_or_404(self, user_id: str) -> User:
        result = await self.db.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise NotFoundException("User", resource_id=user_id)
        return user

    async def _load_user_or_none(self, user_id: str) -> User | None:
        result = await self.db.execute(select(User).where(User.user_id == user_id))
        return result.scalar_one_or_none()

    async def _load_request_or_404(self, request_id: UUID) -> ErasureRequest:
        result = await self.db.execute(
            select(ErasureRequest).where(ErasureRequest.id == request_id)
        )
        request = result.scalar_one_or_none()
        if request is None:
            raise ErasureRequestNotFoundError()
        return request

    async def _find_active_request(self, user_id: str) -> ErasureRequest | None:
        """Active = pending OR cooling_off OR in_progress."""
        result = await self.db.execute(
            select(ErasureRequest)
            .where(
                ErasureRequest.user_id == user_id,
                ErasureRequest.status.in_([STATUS_PENDING, STATUS_COOLING_OFF, STATUS_IN_PROGRESS]),
            )
            .order_by(ErasureRequest.requested_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_active_request_for_user(self, user_id: str) -> ErasureRequest | None:
        """Public read-only accessor for the route handler."""
        return await self._find_active_request(user_id)
