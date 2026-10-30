"""Account erasure orchestrator (Issue #360, GDPR Art.17 / APPI 第22条).

Owns the full lifecycle of an `erasure_requests` row:

    self-service:  pending -> cooling_off -> in_progress -> complete
    admin:                                   in_progress -> complete

Both paths converge on `_execute`, which performs the multi-step cross-store
deletion (Qdrant -> workspace transfer -> Postgres (incl. audit-log
pseudonymize) -> Redis -> audit row -> finalize) covered by the design pin
in #360. The subject's live connector write keys are not deleted with their
other keys: they serve the workspace, so the Postgres step hands them to the
workspace's owner (#2305, ``services.connector_key_ownership``).

Failure semantics: ``_execute`` is NOT a single atomic transaction.
Each step commits when it completes (workspace ownership transfers,
Postgres deletes with the audit-log pseudonymization, audit-row insert,
finalize),
so a failure in step N leaves steps 1..N-1 already persisted. This is
deliberate — partial progress is recorded in ``deleted_data_summary``
and is what ops needs for manual reconciliation. On any raise:

- the in-flight transaction is rolled back (so the failing step's
  partial mutations don't escape),
- the request row is marked ``failed`` with ``failure_reason`` via a
  fresh ``UPDATE`` (independent of the rolled-back ORM state),
- ``deleted_data_summary`` captures whatever steps did complete,
- the exception is re-raised.

A failure AFTER the Postgres step committed (#2268) cannot be retried — the
user row is gone — and the subject is erased in Postgres: the row is marked
``failed`` with ``FAILURE_AFTER_POSTGRES_COMMIT`` in ``failure_reason`` and
its own PII is pseudonymized as ``_finalize`` would, so the re-run
(``reprocess_erasures``) and later erasures treat it as an erasure that ran.

A crash after that commit that no handler sees (SIGTERM, OOM, a cancelled
task, #2286) leaves the row ``in_progress``. The Postgres step writes its
``postgres`` summary on the request row inside its own transaction, so the
sweep, finding the user row gone, can tell such a run from one that never
started: it marks it ``failed`` with ``INTERRUPTED_AFTER_POSTGRES_COMMIT`` and
pseudonymizes it like the handler would, then patches the vector payloads of
the memories the run's restore put back (#2302; the ``account_erasure`` audit
row and the completion email are not done on that path). The ``postgres``
summary decides, not the user lookup: a user row with the sub of such a
request is an account registered again since, and is never erased by it.

The vector-store delete (step 1, ``delete_user_points``) is NOT best-effort:
any failure of either backend (Qdrant, or LanceDB on Kagura Lite) is raised
as ``QdrantError``, and the erasure stops before anything else is touched.
Nothing has been erased in Postgres at that point (the points of the
collections deleted before the failing one may be gone) and the user row
exists, so the request is rescheduled (#2282): back to ``cooling_off`` with
``scheduled_for`` = now + ``VECTOR_STORE_RETRY_DELAY`` and the attempt count
in ``deleted_data_summary.retry``; the hourly sweep runs it again and
``ErasureRetryScheduledError`` (503) tells the caller; a self-service
request can still be cancelled meanwhile, an admin one cannot. After
``VECTOR_STORE_MAX_ATTEMPTS`` attempts the request is ``failed`` with
``vector_store_unavailable after N attempts: ...`` (raw ids kept, like any
failure before the Postgres commit); a new request is the retry. When such a
request is cancelled, or fails after its last attempt, the subject's live
embedded memories are re-queued for embedding (#2304). Any other
error at step 1 (a configuration error building the client, say) fails the
request at once, as before. A request whose step 1 succeeded, or that spent
an earlier attempt, and that then fails before the Postgres commit
(``workspace_transfer_required``, an error inside the Postgres step) re-queues
them the same way (#2358): the points may be gone and the account stays.

Steps 2 and 3 hold the subject's ``users`` row (#2418), so they never wait on
a workspace row: the subject's own workspace delete locks the workspace and
then updates that ``users`` row. They take the live workspace rows ``NOWAIT``
(the order is in ``identity_link_service.lock_identity_links``); a busy one,
or a deadlock this run is the victim of, rolls the step back and reschedules
the request like a vector-store failure, on the same attempt budget, until it
fails with ``lock_conflict after N attempts: ...``.

The Postgres step takes every surviving memory of the subject out of the
embedding queue (#2363; a ``success`` row too since #2393): they are marked
``failed`` with ``ERASED_AUTHOR_EMBEDDING_ERROR`` and no retry budget, so the
sweep does not write the points step 1 deleted again under the pseudonym;
the durable suppression marker also excludes model switches, chunk repair,
and Sleep. Only an explicit admin ``include_erased=true`` restores the scrubbed
pseudonym; context merges inherit that policy. UUID-only cleanup jobs captured
before cascade remove late writes after commit and are verified periodically,
even after completion, to cover a writer crash before its post-write check.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import (
    JSON,
    and_,
    case,
    delete,
    func,
    null,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.session import get_session_manager
from auth.workspace_roles import WorkspaceRole
from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR, MAX_EMBEDDING_RETRIES
from config.settings import get_settings
from db.qdrant import delete_user_points
from db.redis import clear_co_activations, clear_user_rate_limits, get_redis_client
from models.agent import Agent, AgentContextBinding
from models.auth import (
    APIKey,
    AuditLog,
    Context,
    ContextMember,
    ExternalAPIKey,
    OAuth2Client,
    OAuthGrantSetting,
    User,
    UserKnownDevice,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.erasure import (
    FAILURE_AFTER_POSTGRES_COMMIT,
    INTERRUPTED_AFTER_POSTGRES_COMMIT,
    NOTE_PSEUDONYMIZED_KEY,
    NOTE_USER_ROW_ABSENT,
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
from models.memory import MEMORY_TYPE_TIME, Memory, MemoryRevision
from services.billing_contract import (
    ensure_no_billing_contract,
    owned_workspaces_under_contract,
    workspaces_under_contract,
)
from services.connector_key_ownership import reown_connector_keys
from services.email_service import EmailService, get_email_service
from services.erasure_details import (
    ERASURE_AUDIT_ACTION,
    RestoreReport,
    StripCounts,
    Tenure,
    count_audit_mentions_kept,
    email_holdings,
    erase_subject_details,
    erased_pseudonyms,
    erasure_pseudonym,
    lock_subject_memories,
    locked_ids,
    memory_scrub_at,
    merged_request_summary,
    payload_counts,
    pseudonymize_audit_trail,
    pseudonymize_revision_actor,
    pseudonymized_request_values,
    ran_in_postgres,
    record_resynced_payloads,
    restored_payload_syncs,
    strip_subject_details,
    subject_memory_ids,
    sync_restored_payloads,
)
from services.oauth_grant_revocation import revoke_oauth_grants
from services.system_admin_service import SystemAdminService
from services.workspace_locks import lock_workspace_for_update
from utils.datetime import to_utc_iso, utcnow
from utils.db_helpers import is_deadlock
from utils.exceptions import (
    BillingContractActiveError,
    ConflictError,
    EmailDispatchError,
    ErasureAlreadyInProgressError,
    ErasureForbiddenError,
    ErasureRequestNotFoundError,
    ErasureRetryScheduledError,
    ErasureTokenInvalidError,
    InitialAdminCannotBeErasedError,
    NotFoundException,
    QdrantError,
    ValidationError,
    WorkspaceTransferRequiredError,
)
from utils.hashing import sha256_hex
from utils.logger import get_logger
from utils.time_trigger import TRIGGER_WINDOW_KEYS
from utils.utf8 import is_utf8_encodable

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

# #1678: appended to the Redis value (``<request_id><suffix>``) when the token
# was delivered by email. The emailed link is then the second factor by
# itself, decided at request time: setting a password before clicking it must
# not make the link demand one. A bare request id (a password request, or a
# key written before this change) keeps the confirm-time password check.
_EMAIL_LINK_SUFFIX = ":email-link"

# Timeout for the OAuth confirmation-email dispatch. Chosen so a stalled
# email provider cannot hold a DB transaction open and exhaust the
# connection pool: Resend p99 latency is well under 5s in practice;
# 10s tolerates regional spikes while bounding the worst case. On
# timeout, ``asyncio.TimeoutError`` is caught by the existing
# ``except Exception as exc`` handler and treated as a dispatch failure
# (rollback + Redis cleanup + ``EmailDispatchError``). Future: expose
# via ``Settings`` if traffic patterns demand tuning.
CONFIRMATION_EMAIL_TIMEOUT_SECONDS = 10.0

# #2282: a vector-store failure at step 1 reschedules the request this far
# ahead. The sweep is hourly, so the effective gap is up to an hour; the delay
# only keeps a sweep that is still running from picking the row up again.
VECTOR_STORE_RETRY_DELAY = timedelta(minutes=15)

# #2282: attempts (the first run included) before a vector-store outage fails
# the request for good: three retries, roughly three to four hours.
VECTOR_STORE_MAX_ATTEMPTS = 4

# #2282: the ``step`` a retryable failure must happen at, and the summary key
# that carries the attempts across runs.
_STEP_VECTOR_STORE = "vector_store"
_RETRY_KEY = "retry"

# #2418: a lock another transaction holds at steps 2-3 reschedules the request
# like a vector-store failure, on the same attempt budget. The class names in
# ``summary.retry.error_class`` (``55P03`` lock_not_available / ``40P01``).
_PG_LOCK_NOT_AVAILABLE = "55P03"
LOCK_CONFLICT_ERROR_CLASS = "LockNotAvailable"
DEADLOCK_ERROR_CLASS = "DeadlockDetected"
# the ``failure_reason`` prefix once the budget is spent on lock conflicts
LOCK_CONFLICT_GAVE_UP = "lock_conflict after "

# #2304: rows per re-queue statement after a cancelled or failed vector-store
# retry — bounds the lock set of each UPDATE; one commit per batch.
EMBEDDING_REQUEUE_BATCH = 500

# #2304: the longest a re-queue batch waits for a memory row another
# transaction holds; past it the re-queue stops and is logged (the runbook's
# manual statement finishes it). Bounds a member's cancel request.
_REQUEUE_LOCK_TIMEOUT_SQL = text("SET LOCAL lock_timeout = '5s'")

# #2304: keys inside ``summary.retry`` recording the re-queue.
_REQUEUED_KEY = "embeddings_requeued"
_REQUEUED_AT_KEY = "requeued_at"


@dataclass(frozen=True)
class PostgresErasure:
    """What ``_delete_postgres`` did (#2266).

    Attributes:
        counts: The ``postgres`` section of the erasure summary.
        restore: The details restore; its ``to_sync`` are the payloads
            ``sync_restored_payloads`` patches after the commit.
        memory_scrub_at: When the subject's memories were locked (#2268):
            every revision row the erasure saw was written before it, and any
            later edit of those memories waited for the commit. Stored in the
            summary as ``memory_scrub_at``; ``reprocess_erasures`` bounds the
            authored arm of its strip by it.
    """

    counts: dict[str, int]
    restore: RestoreReport
    memory_scrub_at: datetime


def _trigger_only_details() -> Any:
    """What the memory scrub leaves in ``details`` (#2266).

    ``{"trigger": <its window>}`` on a ``type='time'`` row that has a trigger
    — the window keys only (``TRIGGER_WINDOW_KEYS``): the trigger's ``text``
    and any key the author passed through are personal payload like the
    rest — and NULL on every other row. ``details`` is ``json`` (not
    ``jsonb``), so the object is built with ``json_build_object``.
    """
    trigger = Memory.details["trigger"]
    window = func.json_build_object(
        *(part for key in TRIGGER_WINDOW_KEYS for part in (key, trigger[key]))
    )
    return case(
        (
            and_(Memory.type == MEMORY_TYPE_TIME, trigger.is_not(None)),
            func.json_build_object("trigger", window),
        ),
        else_=null(),
    ).cast(JSON)


# Salt used when pseudonymizing audit_log rows for an erased user. A
# stable per-deployment salt (not per-row) lets ops cross-correlate audit
# rows belonging to the same erased user without revealing the email/sub.
# Goal is irreversibility, not rainbow-table protection — once the user
# row is gone there is no plaintext to recover.
def _audit_salt() -> str:
    return get_settings().audit_pseudo_salt


_sha256_hex = sha256_hex  # backward-compat alias for tests + this module


def _previous_retry(summary: dict[str, Any] | None) -> tuple[dict[str, Any] | None, int]:
    """The ``retry`` block an earlier attempt left on the request (#2282),
    and how many attempts failed before this one (0 without a block, or
    with an unreadable count)."""
    retry = (summary or {}).get(_RETRY_KEY)
    if not isinstance(retry, dict):
        return None, 0
    attempts = retry.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        return retry, 0
    return retry, attempts


def _retry_block(
    attempts: int, error_class: str, *, step: str = _STEP_VECTOR_STORE
) -> dict[str, Any]:
    """``summary.retry`` after a vector-store failure (#2282) or a lock
    conflict at steps 2-3 (#2418): counts, the step and the exception class
    only — no account id, no exception text."""
    return {
        "attempts": attempts,
        "step": step,
        "error_class": error_class,
        "last_attempt_at": to_utc_iso(utcnow()),
    }


class _LockConflict(ConflictError):
    """#2418: a row erasure steps 2-3 do not wait for is held by another
    transaction (a ``NOWAIT`` lock lost its race: ``55P03``), or Postgres
    broke a lock cycle with this transaction as the victim (``40P01``).
    The step's transaction is rolled back with nothing of it committed, so
    the request is rescheduled (``ErasureRetryScheduledError``)."""

    def __init__(self, error_class: str) -> None:
        super().__init__("Another operation holds the account's rows; the erasure runs again")
        self.error_class = error_class


def _as_lock_conflict(exc: BaseException) -> _LockConflict | None:
    """``exc`` as a :class:`_LockConflict` when it is one — raised by the
    steps themselves, or a ``55P03`` / ``40P01`` from Postgres — else None."""
    if isinstance(exc, _LockConflict):
        return exc
    if is_deadlock(exc):
        return _LockConflict(DEADLOCK_ERROR_CLASS)
    if isinstance(exc, DBAPIError) and _sqlstate(exc) == _PG_LOCK_NOT_AVAILABLE:
        return _LockConflict(LOCK_CONFLICT_ERROR_CLASS)
    return None


def _sqlstate(exc: DBAPIError) -> str | None:
    """The SQLSTATE of a driver error (asyncpg ``sqlstate``, psycopg ``pgcode``)."""
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


def _requeue_block(summary: dict[str, Any]) -> dict[str, Any]:
    """The ``summary.retry`` block a re-queue record merges into (#2358).

    This run's (or an earlier attempt's) ``retry`` block when the summary has
    one (``_previous_retry``); otherwise ``{"attempts": 0}`` — step 1
    succeeded at its first try and the request failed later, before the
    Postgres commit (``failure_reason`` says why). One key for every re-queue
    record, so the runbook's rule stays "``embeddings_requeued`` next to the
    attempts".
    """
    retry, _ = _previous_retry(summary)
    return retry if retry is not None else {"attempts": 0}


# #2302: what the sweep's close of a claimed request did
CloseOutcome = Literal["interrupted", "never_ran", "moved"]


# #2302: moved to ``erasure_details`` (the re-run writes the summary too)
_merged_summary = merged_request_summary


async def requeue_subject_embeddings(
    db: AsyncSession, user_id: str, *, batch: int = EMBEDDING_REQUEUE_BATCH
) -> int:
    """Put the subject's live embedded memories back to ``pending`` (#2304).

    For a request whose vector-store step ran at least once and that then
    ended without erasing the account (cancelled, or failed after its last
    attempt): ``delete_user_points`` may have removed some or all of the
    subject's points, so their memories would stay ``success`` in Postgres and
    never be found by a search. Only the subject's own rows can have lost a
    point (the point payload's ``user_id`` mirrors ``memories.user_id``; a
    resource-indexer row's author is the context's creator, who is also its
    point's ``user_id``), so the scan is one indexed predicate over them.

    ``success`` and ``processing`` rows: a run in flight may have written its
    point before the delete removed it and would then stamp ``success``;
    moving its row to ``pending`` fences that run off (its outcome writes
    match on ``processing``) and the sweep embeds the row again, as the chunk
    backfill's ``include_in_flight`` does (#2095). A stale ``processing`` row
    with no retry budget left stays the sweep's to fail. A ``pending`` row
    gets its point from the queued run (the write is an upsert); a
    ``failed`` row keeps its own retry budget; a tombstone has no point by
    design. Resource-indexer rows are included: the embedding pipeline
    rebuilds their indexer point from the row (``write_memory_point``).

    Writes ``embedding_status`` only, like ``requeue_embedding``:
    ``updated_at`` and the pipeline's attempt/error/retry fields stay. Each
    batch locks its rows in id order (``FOR NO KEY UPDATE`` subquery, no SKIP
    LOCKED — a skipped row would be missed), waits at most 5 s for a row
    another transaction holds (``lock_timeout``; the error ends the re-queue),
    and commits, so a long re-queue never holds a large lock set. Batches
    page on the id (keyset), so a row the sweep re-embeds meanwhile is not
    queued twice; the loop ends on a short batch.

    Cost: one embedding call per re-queued memory (plus its chunks) — what
    the runbook's manual statement costs. The embedding sweep orders
    ``pending`` rows by ``created_at``, so a large re-queue is embedded
    before newer rows of other members.

    Returns:
        How many rows were re-queued (0 on a second call).
    """
    from services.embedding_claim import STALE_CLAIM_SECONDS
    from services.memory_service import embedding_reclaim_exhausted_clause

    total = 0
    after: UUID | None = None
    while True:
        stale_cutoff = utcnow() - timedelta(seconds=STALE_CLAIM_SECONDS)
        conditions = [
            Memory.user_id == user_id,
            Memory.deleted_at.is_(None),
            Memory.embedding_status.in_(("success", "processing")),
            ~embedding_reclaim_exhausted_clause(stale_cutoff),
        ]
        if after is not None:
            conditions.append(Memory.id > after)
        locked = (
            select(Memory.id)
            .where(*conditions)
            .order_by(Memory.id)
            .limit(batch)
            .with_for_update(key_share=True)
        )
        await db.execute(_REQUEUE_LOCK_TIMEOUT_SQL)
        moved = list(
            (
                await db.execute(
                    update(Memory)
                    .where(Memory.id.in_(locked))
                    .values(embedding_status="pending")
                    .returning(Memory.id)
                    .execution_options(synchronize_session=False)
                )
            )
            .scalars()
            .all()
        )
        await db.commit()
        total += len(moved)
        if len(moved) < batch:
            return total
        after = max(moved)


class _StaleErasureRun(Exception):
    """A prior run erased this request's subject, or its exact user row is gone."""


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
            BillingContractActiveError: User owns a workspace with a running paid
                subscription (409 ``BILLING-005``, #1940).
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

        # Issue #1940: no erasure while the user owns a workspace with a running
        # subscription. Refuse before any row, token or email is created.
        ensure_no_billing_contract(await owned_workspaces_under_contract(self.db, user_id))

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
        #    rollback PG + best-effort Redis key delete + structured
        #    log of "commit_failed_after_side_effects". The OAuth user
        #    has already received the confirm email; if they click, the
        #    confirm endpoint hits _load_request_or_404 → 404 (existing
        #    ErasureRequestNotFoundError path), so the user sees a
        #    legitimate "request not found" rather than confirming a
        #    rolled-back row. Rare in practice but explicitly handled
        #    so ops can correlate the (rare) ghost-link reports.
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ErasureAlreadyInProgressError("active") from exc

        # #1678: the channel follows whether the account HAS a password, not
        # its original sign-in method — an OAuth account that added a password
        # re-enters it like any password user, and only a passwordless account
        # needs the emailed link as its second factor.
        is_oauth = target.password_hash is None

        redis = get_redis_client()
        try:
            await redis.setex(
                f"{_CONFIRM_TOKEN_KEY_PREFIX}{token}",
                CONFIRM_TOKEN_TTL_SECONDS,
                f"{request.id}{_EMAIL_LINK_SUFFIX}" if is_oauth else str(request.id),
            )
        except Exception:
            await self.db.rollback()
            raise

        if is_oauth:
            try:
                # ``.strip()`` defends against trailing whitespace in env vars
                # (a common deploy hazard); ``.rstrip("/")`` normalizes the
                # trailing slash so the f-string below produces a single slash.
                # Both URL construction AND the email send are inside this
                # try so that ANY failure after Redis SETEX (settings access,
                # f-string interp, email dispatch) routes through the same
                # rollback + Redis-cleanup + ``EmailDispatchError`` path. A
                # bare exception escaping here would leave an open transaction
                # plus an orphan Redis token (until 1h TTL).
                # Boot-time validation (non-empty + http(s)-prefixed) is
                # tracked separately in #480 alongside RESEND_DPA_ACCEPTED_AT.
                base_url = get_settings().frontend_url.strip().rstrip("/")
                confirm_url = f"{base_url}/account/erasure/confirm?token={token}"
                sent = await asyncio.wait_for(
                    self.email_service.send_erasure_confirmation(
                        to_email=target.email,
                        request_id=str(request.id),
                        confirm_token=token,
                        confirm_url=confirm_url,
                    ),
                    timeout=CONFIRMATION_EMAIL_TIMEOUT_SECONDS,
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

        try:
            await self.db.commit()
        except Exception as exc:
            # Commit failure after side-effects (Redis SETEX done; for
            # OAuth users, confirmation email already sent). Rare —
            # network blip during transaction commit, deadlock, etc. —
            # but worth distinguishing from earlier failure modes for
            # ops diagnosis. Best-effort cleanup so the token doesn't
            # survive on Redis: a clicked confirm link would 404 against
            # ``_load_request_or_404`` (no row), which is the existing
            # ErasureRequestNotFoundError path. Log redaction follows
            # the same discipline as the email-dispatch failure path:
            # ``error_type`` only, never ``str(exc)``.
            try:
                await self.db.rollback()
            except Exception:
                pass
            try:
                await redis.delete(f"{_CONFIRM_TOKEN_KEY_PREFIX}{token}")
            except Exception:
                pass
            logger.error(
                "erasure_request_commit_failed_after_side_effects",
                request_id=str(request.id),
                user_id=user_id,
                auth_method=target.auth_method,
                email_sent=is_oauth,
                error_type=type(exc).__name__,
            )
            raise
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
            BillingContractActiveError: User owns a workspace with a running paid
                subscription (409 ``BILLING-005``, #1940).
        """
        target = await self._load_user_or_404(user_id)

        # #1718: a token that cannot be UTF-8 encoded was never issued, and it
        # would raise as a Redis key. (Such a password is simply a mismatch in
        # ``verify_password``.)
        if not is_utf8_encodable(token):
            raise ErasureTokenInvalidError()

        # Resolve and validate token via Redis (raw token never on disk).
        redis = get_redis_client()
        redis_key = f"{_CONFIRM_TOKEN_KEY_PREFIX}{token}"
        request_id_str = await redis.get(redis_key)
        if not request_id_str:
            raise ErasureTokenInvalidError()
        # The token was emailed: the link is the proof (see _EMAIL_LINK_SUFFIX).
        via_email_link = request_id_str.endswith(_EMAIL_LINK_SUFFIX)
        if via_email_link:
            request_id_str = request_id_str.removesuffix(_EMAIL_LINK_SUFFIX)

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
        # session cookie was the first. Which one applies was decided when
        # the erasure was requested (#1678): an emailed link stays enough even
        # if a password was set since.
        if not via_email_link and target.password_hash is not None:
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
        # Issue #1940: same best-effort pre-check for a subscription started (or
        # resumed) since the request; the sweep re-checks before executing.
        # The request is cancelled (and its token burned) so the user can start
        # a fresh one once the contract has ended, instead of meeting
        # "already requested" for the life of a dead pending row.
        under_contract = await owned_workspaces_under_contract(self.db, user_id)
        if under_contract:
            request.status = STATUS_CANCELLED
            request.cancelled_at = utcnow()
            await self.db.commit()
            try:
                await redis.delete(redis_key)
            except Exception as exc:
                # Best-effort: the row is already cancelled, so a leftover token
                # cannot confirm anything; keep the 409 instead of a 500.
                logger.warning(
                    "erasure_confirm_token_delete_failed",
                    request_id=str(request.id),
                    error=str(exc),
                )
            ensure_no_billing_contract(under_contract)

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
            scheduled_for_iso=to_utc_iso(request.scheduled_for) or "",
        )

        logger.info(
            "erasure_request_confirmed",
            request_id=str(request.id),
            user_id=user_id,
            scheduled_for=to_utc_iso(request.scheduled_for),
        )
        return request

    async def cancel_self_service(self, *, user_id: str) -> ErasureRequest:
        """Cancel a pending or cooling_off request.

        Both states are cancellable:
        - ``pending``: user requested but never confirmed. The Redis token
          may have already expired (1h TTL); without this allow-cancel, a
          forgotten pending row would block all future erasure requests
          from this user (partial unique index treats pending as active).
        - ``cooling_off``: user confirmed but the 7-day window hasn't elapsed,
          or a rescheduled self-service request waits for its retry (#2282).

        Only the subject's own (self-service) requests: an admin force-erase
        is ``cooling_off`` too while it waits for a vector-store retry
        (#2282), and the erased user must not be able to call it off.

        A request that already spent a vector-store attempt
        (``summary.retry.attempts > 0``) may have deleted some of the
        subject's points: once the cancel is committed, the subject's live
        embedded memories go back to ``pending`` (#2304,
        ``_requeue_after_points_deleted``; never fails the cancel).

        ``in_progress`` rows are NOT cancellable — the orchestrator is
        already running, and racing it would risk an inconsistent half-
        applied erasure. The cancel is a compare-and-set on the status
        (``WHERE status IN (pending, cooling_off)``): a sweep that claimed
        the row between the read and the write (up to once per retry,
        #2282) wins, and the cancel answers 409 instead of overwriting the
        running request.

        A lost compare-and-set re-reads the row as the caller's (#2306):
        ``cancelled`` (a concurrent cancel of the same request won, e.g. a
        second tab) returns it like a first cancel does; ``in_progress`` is
        409; a row that ended (``complete`` / ``failed``), is gone, or is no
        longer the caller's (a finished erasure pseudonymizes its
        ``user_id``) is 404 — there is nothing left to cancel.

        Raises:
            ErasureRequestNotFoundError: No active cancellable request, or
                the row ended or is no longer the caller's after a lost
                compare-and-set.
            ErasureAlreadyInProgressError: The row left ``pending`` /
                ``cooling_off`` after it was read (claimed by the sweep).
        """
        request = await self._find_active_request(user_id)
        if (
            request is None
            or not request.is_self_service
            or request.status not in (STATUS_PENDING, STATUS_COOLING_OFF)
        ):
            raise ErasureRequestNotFoundError(user_id)

        cancelled_from = request.status
        request_id = request.id
        result = await self.db.execute(
            update(ErasureRequest)
            .where(
                ErasureRequest.id == request_id,
                ErasureRequest.status.in_([STATUS_PENDING, STATUS_COOLING_OFF]),
            )
            .values(status=STATUS_CANCELLED, cancelled_at=utcnow())
            # #2304: the summary of the row this cancel moved, not of the
            # one read above — a run that rescheduled the request in
            # between may have spent an attempt
            .returning(ErasureRequest.deleted_data_summary)
            # no in-session evaluation: the loaded row may be stale (that
            # is the race); the refresh below reads what was written
            .execution_options(synchronize_session=False)
        )
        moved = result.first()  # None: the compare-and-set matched nothing
        await self.db.commit()
        if moved is None:
            # moved since the read: answer by what it is now, read as the
            # caller's (#2306)
            current = (
                await self.db.execute(
                    select(ErasureRequest)
                    .where(ErasureRequest.id == request_id, ErasureRequest.user_id == user_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            logger.info(
                "erasure_request_cancel_lost",
                request_id=str(request_id),
                status=current.status if current is not None else None,
            )
            if current is not None and current.status == STATUS_CANCELLED:
                # a concurrent cancel of this request won: same answer
                return current
            if current is None or current.status in (STATUS_COMPLETE, STATUS_FAILED):
                # ended or gone: nothing to cancel
                raise ErasureRequestNotFoundError(user_id)
            # ``in_progress``; or back in ``pending`` / ``cooling_off`` only
            # after a claim beat this cancel and the run rescheduled it
            # (#2282): report the claim
            raise ErasureAlreadyInProgressError(STATUS_IN_PROGRESS)
        # #2304: a request that spent a vector-store attempt may have deleted
        # the subject's points
        spent_retry, spent_attempts = _previous_retry(
            moved.deleted_data_summary if moved is not None else None
        )
        if spent_retry is not None and spent_attempts > 0:
            await self._requeue_after_points_deleted(
                request_id, user_id, spent_retry, status=STATUS_CANCELLED
            )
        await self.db.refresh(request)

        logger.info(
            "erasure_request_cancelled",
            request_id=str(request.id),
            user_id=user_id,
            cancelled_from=cancelled_from,
        )
        return request

    async def _requeue_after_points_deleted(
        self, request_id: UUID, user_id: str, retry: dict[str, Any], *, status: str
    ) -> None:
        """Re-queue the subject's embeddings after a request that may have
        deleted their points ended without an erasure.

        Three callers: a cancel after a spent vector-store attempt (#2304),
        the give-up after the last attempt (#2304), and any other failure
        before the Postgres commit that follows a completed step 1 or an
        earlier attempt (#2358 — a workspace-transfer error, an error inside
        the Postgres step, a billing contract on a retry run). A request
        whose step 1 never failed has no ``retry`` block; ``retry`` is then
        ``{"attempts": 0}`` (``_requeue_block``).

        Called after the terminal status (``cancelled`` / ``failed``) is
        committed, in its own transactions: ``requeue_subject_embeddings``,
        then the count merged into ``summary.retry`` on the request row
        (guarded by id + ``status``). Never raises: the cancel's answer and
        the failure's own error stand; a failed re-queue is logged for the
        runbook's manual statement. The logs carry no account id.
        """
        try:
            requeued = await requeue_subject_embeddings(self.db, user_id)
            block = {
                **retry,
                _REQUEUED_KEY: requeued,
                _REQUEUED_AT_KEY: to_utc_iso(utcnow()),
            }
            result = cast(
                CursorResult[Any],
                await self.db.execute(
                    update(ErasureRequest)
                    .where(ErasureRequest.id == request_id, ErasureRequest.status == status)
                    .values(deleted_data_summary=_merged_summary({_RETRY_KEY: block}))
                    .execution_options(synchronize_session=False)
                ),
            )
            await self.db.commit()
        except Exception as exc:
            try:
                await self.db.rollback()
            except Exception:  # noqa: S110 - the connection is gone; the session resets on next use
                pass
            logger.error(
                "erasure_embeddings_requeue_failed",
                request_id=str(request_id),
                error_class=type(exc).__name__,
            )
            return
        if not result.rowcount:
            # a terminal row does not move; defensive
            logger.warning(
                "erasure_embeddings_requeue_unrecorded",
                request_id=str(request_id),
                memories=requeued,
            )
            return
        logger.info(
            "erasure_embeddings_requeued",
            request_id=str(request_id),
            memories=requeued,
        )

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
            ErasureRetryScheduledError: The vector store failed at the first
            step (#2282); nothing was erased in Postgres and the request is
            now ``cooling_off`` with ``scheduled_for`` set — the first hourly
            sweep after it runs it again, a second force-erase for the user
            meanwhile answers ``ErasureAlreadyInProgressError``, and the
            subject cannot cancel it (``cancel_self_service``).
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

        A request rescheduled after a vector-store failure (#2282) is one of
        these ``cooling_off`` rows again; when ``_execute`` reschedules it once
        more, the sweep logs ``erasure_sweep_retry_scheduled`` and goes on.

        Also handles two stale-state cleanup paths:

        - **Stale ``in_progress``** (``started_at`` older than 1h): these
          are runs that were marked in-progress but did not complete
          (process SIGTERM/OOM between commit and ``_execute``). Picked
          up by the main sweep query and re-executed while the user row
          exists. When it is gone (#2286), the request is never re-run:
          a summary with ``postgres`` (written in the Postgres step's own
          transaction) means that step committed and the run was
          interrupted after it — ``failed`` with
          ``INTERRUPTED_AFTER_POSTGRES_COMMIT``, the subject's points deleted
          once more (step 3a, #2363) and its Redis state cleared, both
          best-effort, and the vector payloads of the memories its
          restore put back are patched right after (#2302,
          ``_resync_interrupted_payloads``); without it nothing ran —
          ``complete`` with the ``user_row_already_absent`` note. Either way
          the row is pseudonymized (``pseudonymized_request_values``). A row
          with ``postgres`` is closed that way even when a user row with its
          sub exists (#2302): the subject was deleted in that transaction,
          so the user row is an account registered again since — it is never
          run against, and its points and Redis keys are kept.

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
        stale_pending_result = cast(
            CursorResult[Any],
            await self.db.execute(
                update(ErasureRequest)
                .where(
                    (ErasureRequest.status == STATUS_PENDING)
                    & (ErasureRequest.requested_at <= pending_token_expired_cutoff)
                )
                .values(status=STATUS_CANCELLED, cancelled_at=now)
            ),
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
        # also see them as cooling_off. The ids are read first, while the
        # rows are fresh: a failed ``_execute`` rolls the session back, which
        # expires every loaded instance, and an async session cannot
        # lazy-load an expired attribute (MissingGreenlet, #2266).
        due = [(req.id, req.user_id) for req in due_requests]
        for req in due_requests:
            req.status = STATUS_IN_PROGRESS
            req.started_at = now
        await self.db.commit()

        executed = 0
        for request_id, user_id in due:
            # One SELECT each (≤ batch_size rows per sweep): both rows are
            # read after any earlier request's rollback, never from an
            # expired instance. Handlers below log the locals only.
            request = (
                await self.db.execute(select(ErasureRequest).where(ErasureRequest.id == request_id))
            ).scalar_one_or_none()
            if request is None:
                continue  # removed since it was claimed; nothing to run
            # #2302: decided before the user lookup. ``postgres`` is written in
            # the Postgres step's transaction, with the user delete: on a
            # claimed row it proves this request's subject was erased, so a
            # user row with that sub now is another account (registered again
            # since) and must never be run against.
            summary = dict(request.deleted_data_summary or {})
            ran = isinstance(summary.get("postgres"), dict)
            target = await self._load_user_or_none(user_id)
            if ran or target is None:
                try:
                    closed = await self._close_request_without_user(
                        request, user_row_exists=target is not None
                    )
                except Exception as exc:
                    # one request's close must not stop the batch (the others
                    # are claimed already); the request id and the class only
                    await self.db.rollback()
                    logger.error(
                        "erasure_sweep_close_failed",
                        request_id=str(request_id),
                        error_class=type(exc).__name__,
                    )
                    continue
                if closed == "interrupted":
                    try:
                        # #2302: the run's payload patch of restored memories
                        await self._resync_interrupted_payloads(
                            request_id, self._pseudonym(user_id), summary
                        )
                    except Exception as exc:
                        # the row is closed (terminal) already: only
                        # ``reprocess_erasures`` re-drives it now (no
                        # ``details_payload_synced`` key on the row)
                        try:
                            await self.db.rollback()
                        except Exception:  # noqa: S110 - the connection is gone; the session resets on next use
                            pass
                        logger.error(
                            "erasure_sweep_payload_resync_failed",
                            request_id=str(request_id),
                            error_class=type(exc).__name__,
                        )
                continue
            try:
                await self._execute(request, target)
                executed += 1
            except BillingContractActiveError as exc:
                # Expected refusal (#1940), not a fault: `_execute` already
                # marked the row failed with the reason. No stack trace/alert.
                logger.warning(
                    "erasure_sweep_blocked_by_billing_contract",
                    request_id=str(request_id),
                    user_id=user_id,
                    workspace_ids=exc.details.get("workspace_ids"),
                )
                continue
            except ErasureRetryScheduledError as exc:
                # #2282: the vector store is down; ``_execute`` put the row back
                # to ``cooling_off`` with a later ``scheduled_for`` (so this
                # batch, read above, does not run it again). Not a fault yet:
                # no stack trace; not counted as executed.
                logger.warning(
                    "erasure_sweep_retry_scheduled",
                    request_id=str(request_id),
                    attempt=exc.attempt,
                    max_attempts=exc.max_attempts,
                    scheduled_for=to_utc_iso(exc.scheduled_for),
                )
                continue
            except Exception as exc:
                # #2268 review: the request id and the class only. ``_execute``
                # logged the failure already (with the account and the text
                # only when nothing was erased yet); after the Postgres commit
                # the account id and an exception text must not reach the logs.
                logger.error(
                    "erasure_sweep_execute_failed",
                    request_id=str(request_id),
                    error_class=type(exc).__name__,
                )
                # `_execute` already marked status=failed; continue to
                # the next request in this batch.
                continue
        return executed

    async def _close_request_without_user(
        self,
        request: ErasureRequest,
        *,
        user_row_exists: bool = False,
        claimed_at: datetime | None = None,
    ) -> CloseOutcome:
        """Close a claimed request whose user row is gone (#2286), or whose
        Postgres step committed although a user row with its sub exists
        (#2302: the sub was registered again since).

        ``deleted_data_summary.postgres`` is written in the Postgres step's
        own transaction (``_delete_postgres``), so its presence means the
        subject was erased in Postgres and the run was interrupted after
        that commit, before the handler or ``_finalize`` wrote the row: it
        becomes ``failed`` with ``INTERRUPTED_AFTER_POSTGRES_COMMIT`` (no
        completion time; ``memory_scrub_at`` stays in the summary), so
        ``finalized_erasure`` — the re-run, the erased-members set — treats
        it as an erasure that ran. The subject's points are deleted once more
        (step 3a, ``_delete_late_points``, #2363: the run may have died before
        it) and its Redis state is cleared, both best-effort; the payload
        patch of restored memories is re-driven by the caller right after
        this commit (#2302,
        ``_resync_interrupted_payloads``); the ``account_erasure`` audit row
        and the completion email are not done. When ``user_row_exists`` (the
        sub was registered again after the Postgres commit, #2302), the same
        close is written, but the points and the Redis keys are the new
        account's: they are kept, no ``qdrant_after_commit`` or ``redis`` key
        is added (#2362), and the log line is
        ``erasure_sweep_subject_reregistered``. Without ``postgres`` nothing was erased (the row was removed
        some other way, or the run was interrupted before the Postgres
        commit on a version that wrote the summary only after it): the
        ``complete`` row gets the ``user_row_already_absent`` note, with
        ``pseudonymized: true`` — ``erased_pseudonyms`` then takes the
        stored pseudonym as is (a run interrupted on v0.124.x and swept here
        did erase; its rows carry that pseudonym), where a note row written
        before keeps the raw sub and is derived.

        Either way the row is pseudonymized: the account no longer exists,
        and the raw sub must not outlive it on this row. The UPDATE is
        guarded by ``status = in_progress`` (the claim set it), so a row
        moved meanwhile is left alone and only logged as moved. The log
        lines carry the request id only.

        Returns:
            ``"interrupted"`` (the row is now ``failed`` with the marker),
            ``"never_ran"`` (the note row) or ``"moved"`` (nothing written).

        Raises:
            ValueError: ``user_row_exists`` on a row without ``postgres``: that
                request must run (``_execute``), not be closed.
        """
        request_id = request.id
        user_id = request.user_id
        # What the row holds: ``postgres`` (written in the Postgres step's
        # transaction) and any key written before it. Never ``qdrant`` or
        # ``workspaces``: ``_execute`` keeps those in its local summary until a
        # later write, and they are lost with the interrupted process.
        summary = dict(request.deleted_data_summary or {})
        ran = isinstance(summary.get("postgres"), dict)
        if user_row_exists and not ran:
            raise ValueError("a request that never ran with a live user row is not closed")
        values = pseudonymized_request_values(user_id, request.initiated_by)
        if ran:
            if not user_row_exists:
                # step 3a once more (#2363): the run may have died between the
                # Postgres commit and its late delete, leaving a point written
                # under the raw sub after step 1. Never raises. Not when the
                # sub was registered again: its points are the new account's.
                summary.update(await self._delete_late_points(user_id, request_id))
                redis = await self._clear_redis_best_effort(request_id, user_id)
                if redis is not None:
                    summary["redis"] = redis
            values.update(
                status=STATUS_FAILED,
                failure_reason=INTERRUPTED_AFTER_POSTGRES_COMMIT,
                deleted_data_summary=summary,
            )
        else:
            values.update(
                status=STATUS_COMPLETE,
                completed_at=utcnow(),
                deleted_data_summary={
                    "note": NOTE_USER_ROW_ABSENT,
                    NOTE_PSEUDONYMIZED_KEY: True,
                },
            )
        guard = [ErasureRequest.id == request_id, ErasureRequest.status == STATUS_IN_PROGRESS]
        if claimed_at is not None:
            guard.append(ErasureRequest.started_at == claimed_at)
        if not ran:
            # #2302 review: a run alive past the cutoff may commit its Postgres
            # step (writing ``postgres``) after the sweep read the row; the
            # note must not replace that summary — the next sweep closes the
            # row as interrupted instead
            guard.append(~ran_in_postgres())
        result = cast(
            CursorResult[Any],
            await self.db.execute(update(ErasureRequest).where(*guard).values(**values)),
        )
        await self.db.commit()
        if not result.rowcount:
            logger.info("erasure_sweep_request_moved", request_id=str(request_id))
            return "moved"
        if user_row_exists:
            logger.warning("erasure_sweep_subject_reregistered", request_id=str(request_id))
        else:
            logger.warning(
                "erasure_sweep_user_row_absent", request_id=str(request_id), ran_in_postgres=ran
            )
        return "interrupted" if ran else "never_ran"

    async def _resync_interrupted_payloads(
        self, request_id: UUID, pseudonym: str, summary: dict[str, Any]
    ) -> None:
        """Patch the vector payloads of the memories an interrupted run
        restored (#2302), right after the sweep closed it as interrupted.

        The run's ``RestoreReport`` died with its process, so the restored
        set comes from the restore's revision rows
        (``restored_payload_syncs``, bounded by the run's ``memory_scrub_at``)
        and goes through ``sync_restored_payloads`` — its own group
        transactions after the close's commit, a failed patch re-queues the
        memory for embedding, and it never raises for the vector store's or
        the session's sake. The outcome is recorded as the live run's step 3b
        would (``payload_counts`` in the ``postgres`` section, zeros
        included: ``details_payload_synced`` present means the re-drive ran,
        which is what ``reprocess_erasures`` checks before its backstop),
        guarded by ``status = failed`` and the interrupted marker: when the
        run's own ``_finalize`` landed meanwhile (it clears the marker, and
        patched its own payloads), nothing is written and the row is logged
        as moved. Logs carry the request id and counts only.
        """
        syncs = await restored_payload_syncs(
            self.db, pseudonym, since=memory_scrub_at(summary, strict=False)
        )
        await self.db.rollback()  # ends the read; the patch groups commit their own
        report = RestoreReport()
        await sync_restored_payloads(self.db, syncs, report)
        written = await record_resynced_payloads(self.db, request_id, summary["postgres"], report)
        await self.db.commit()
        if not written:
            logger.info("erasure_sweep_request_moved", request_id=str(request_id))
            return
        logger.info(
            "erasure_sweep_payloads_resynced",
            request_id=str(request_id),
            memories=len(syncs),
            **payload_counts(report),
        )

    async def _clear_redis_best_effort(
        self, request_id: UUID, user_id: str
    ) -> dict[str, int] | None:
        """``_clear_redis`` for an interrupted run (#2286): a failure is logged
        (request id and class only — the subject is erased) and gives None.
        The keys expire by themselves and the sub names no account any more."""
        try:
            return await self._clear_redis(user_id)
        except Exception as exc:
            logger.warning(
                "erasure_sweep_redis_cleanup_failed",
                request_id=str(request_id),
                error_class=type(exc).__name__,
            )
            return None

    # ------------------------------------------------------------------
    # Core execution (multi-step orchestrator)
    # ------------------------------------------------------------------

    async def _execute(self, request: ErasureRequest, target: User) -> None:
        """Run the cross-store erasure for a single request.

        Order: vector store -> workspaces -> Postgres (incl. the audit_logs
        pseudonymize, #2287) -> payloads -> Redis -> audit row -> finalize.
        The vector store first so its failure leaves everything untouched:
        the user row exists and the request can simply run again — a
        ``QdrantError`` there reschedules it (#2282, ``ErasureRetryScheduledError``)
        until ``VECTOR_STORE_MAX_ATTEMPTS``; the last one fails the request
        and re-queues the subject's embeddings (#2304), as does any failure
        after step 1 (or an earlier attempt) and before the Postgres commit
        (#2358). Redis last because
        session/rate-limit data is ephemeral and re-creatable from cookies if
        anything goes wrong.

        Raises:
            ErasureRetryScheduledError: The vector store failed at step 1, or
                a row lock another operation holds stopped step 2 or 3
                (#2418), with attempts left; the row is ``cooling_off`` again.
        """
        summary: dict[str, Any] = {}
        # Read before any failure: the handlers below roll back first, which
        # expires ``request``, and an async session cannot lazy-load the
        # expired id back (MissingGreenlet) — the row would never be marked
        # failed (#2244: a vector-store failure inside the Postgres step).
        request_id = request.id
        # #2304: the subject whose embeddings a give-up re-queues (``target``
        # is expired by the handler's rollback too)
        subject_id = target.user_id
        # #2268: what a failure after the Postgres commit writes on the row —
        # read now, like ``request_id``
        pseudonymized_row = pseudonymized_request_values(request.user_id, request.initiated_by)
        # #2282: earlier vector-store failures of this request. The block is
        # carried in ``summary`` from the start, so every write of the summary
        # below (``_finalize``, each failure handler) keeps the history.
        retry, failed_attempts = _previous_retry(request.deleted_data_summary)
        if retry is not None:
            summary[_RETRY_KEY] = retry
        # #2282: this run's claim (the sweep's, or the force-erase's creation).
        # The reschedule is guarded by it, so a run that outlived the stale
        # cutoff and was claimed again cannot move the newer run's row.
        claimed_at = request.started_at
        # #2282: set when the vector store failed at step 1 on the last attempt
        vector_store_gave_up: str | None = None
        # #2418: set when a lock conflict at steps 2-3 spent the last attempt
        retry_gave_up: str | None = None
        # #2358: step 1 returned — the subject's points may be gone
        points_deleted = False
        erased_in_postgres = False

        def points_may_be_gone() -> bool:
            """#2358: this run's step 1 returned, or an earlier attempt's step 1
            ran (#2282: a partial delete before its ``QdrantError``). A failure
            handler re-queues the subject's embeddings then, as long as the
            Postgres step has not committed. ``vector_store_gave_up`` is
            implied by ``failed_attempts > 0`` whenever
            ``VECTOR_STORE_MAX_ATTEMPTS >= 2``; it is kept for the give-up on
            a first attempt when the constant is 1, so it is not dead code."""
            return points_deleted or failed_attempts > 0 or vector_store_gave_up is not None

        step = "postgres"
        try:
            # #2362: re-read after the sweep's unlocked snapshot, before any
            # external delete. Lock the exact account row, never a new same-sub
            # registration. No request lock yet: requests are locked last.
            await self._guard_erasure_run(target, request_id=request_id)
            owned_workspaces = await self._list_owned_workspaces(target.user_id)

            # Issue #1940: the enforcement point for self-service erasure — a
            # subscription can start during the 7-day cooling-off. Admin
            # force-erase stays the operator's escape hatch (e.g. a plan that
            # billing failed to push back to Free); what it overrides is logged
            # once the erasure has completed. Soft-deleted workspaces are
            # hard-deleted here too, so the log includes them.
            if request.is_self_service:
                ensure_no_billing_contract(owned_workspaces)
                overridden: list[Workspace] = []
            else:
                overridden = workspaces_under_contract(owned_workspaces, include_deleted=True)

            # Stripe customer/subscription teardown is no longer this backend's
            # job (#1096): the OSS backend is Stripe-agnostic. memory-cloud erases
            # only its own data below.

            # Step 1: the vector store (raises on failure -> caught below).
            # #2282: a ``QdrantError`` here (either backend, any transport,
            # timeout or API failure) is the one retryable failure: nothing
            # has been written in Postgres yet, so the request goes back to the
            # sweep's queue while attempts are left; the last one falls through
            # to the generic handler as ``vector_store_unavailable``. The
            # vector delete runs collection by collection, so the points of the
            # collections before the failing one may be gone already; the
            # re-run deletes the rest (deleting nothing is fine).
            step = _STEP_VECTOR_STORE
            try:
                from services.vector_erasure import delete_subject_points

                summary["qdrant"] = await delete_subject_points(self.db, target.user_id)
                points_deleted = True
            except QdrantError as exc:
                attempt = failed_attempts + 1
                summary[_RETRY_KEY] = _retry_block(attempt, type(exc).__name__)
                if attempt < VECTOR_STORE_MAX_ATTEMPTS:
                    raise ErasureRetryScheduledError(
                        request_id=str(request_id),
                        attempt=attempt,
                        max_attempts=VECTOR_STORE_MAX_ATTEMPTS,
                        scheduled_for=utcnow() + VECTOR_STORE_RETRY_DELAY,
                        error_class=type(exc).__name__,
                    ) from exc
                vector_store_gave_up = f"vector_store_unavailable after {attempt} attempts: {exc}"
                raise

            # #2418: steps 2 and 3 hold the subject's ``users`` row and take the
            # workspace rows NOWAIT (the subject's own DELETE /workspaces/W
            # holds W, then updates that ``users`` row). A lock they lose, or
            # a deadlock this run is the victim of, rolls the step back with
            # nothing of it committed: the request is rescheduled like a
            # vector-store failure, on the same attempt budget.
            try:
                # Step 2: workspace ownership transfer / abort gate
                step = "workspaces"
                summary["workspaces"] = await self._handle_owned_workspaces(
                    target.user_id, owned_workspaces
                )

                # Step 3: Postgres deletes (FK-safe order)
                step = "postgres"
                erased = await self._delete_postgres(target, request_id=request_id)
            except (_LockConflict, DBAPIError) as exc:
                conflict = _as_lock_conflict(exc)
                if conflict is None:
                    raise
                attempt = failed_attempts + 1
                summary[_RETRY_KEY] = _retry_block(attempt, conflict.error_class, step=step)
                if attempt < VECTOR_STORE_MAX_ATTEMPTS:
                    raise ErasureRetryScheduledError(
                        request_id=str(request_id),
                        attempt=attempt,
                        max_attempts=VECTOR_STORE_MAX_ATTEMPTS,
                        scheduled_for=utcnow() + VECTOR_STORE_RETRY_DELAY,
                        error_class=conflict.error_class,
                        lock_conflict=True,
                    ) from conflict
                retry_gave_up = (
                    f"{LOCK_CONFLICT_GAVE_UP}{attempt} attempts: {step}: "
                    f"{conflict.error_class} (another operation held the account's rows)"
                )
                raise conflict from exc
            erased_in_postgres = True  # committed: no step below can undo it
            postgres: dict[str, int | str | None] = dict(erased.counts)
            # #2268: the re-run's bound for the subject's authored memories
            postgres["memory_scrub_at"] = to_utc_iso(erased.memory_scrub_at)
            summary["postgres"] = postgres

            # Step 3a (#2391): drain UUID scopes captured in the PG commit.
            # Vector outages leave durable pending jobs, including for rows
            # removed by cascade. The hourly sweep also rechecks retained
            # completed scopes to catch a writer that crashed after upsert.
            summary.update(await self._delete_late_points(target.user_id, request_id))

            # Step 3b (#2266): the restored memories' vector payloads, after
            # the commit — no vector-store call while the subject's memories
            # are locked. A failed patch re-queues the memory for embedding.
            step = "payloads"
            await sync_restored_payloads(self.db, erased.restore.to_sync, erased.restore)
            postgres.update(payload_counts(erased.restore))

            # Step 4 (the audit_logs pseudonymize) runs inside the Postgres
            # step since #2287: ``counts["audit_logs_pseudonymized"]``, and the
            # rows mentioning the subject since #2298:
            # ``counts["audit_mentions_pseudonymized"]``.

            # Step 5: Redis cleanup (best-effort)
            step = "redis"
            summary["redis"] = await self._clear_redis(target.user_id)

            # Step 6: write the new "account_erasure" audit row
            step = "audit_row"
            await self._write_audit_log(request, target, summary)

            # Step 7: finalize the erasure_requests row
            step = "finalize"
            await self._finalize(request, summary)
            if overridden:
                logger.warning(
                    "erasure_admin_override_billing_contract",
                    request_id=str(request.id),
                    user_id=target.user_id,
                    workspace_ids=[str(ws.id) for ws in overridden],
                )

            # Step 8: completion notification — outside the success path's
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

        except _StaleErasureRun:
            # A slow run may have committed after this sweep read the request.
            # Never overwrite its counts or run raw-sub vector/Redis cleanup on
            # a potentially re-registered account. A later durable point job is
            # scoped to the original UUIDs, not the re-used subject.
            await self.db.rollback()
            current = (
                await self.db.execute(
                    select(ErasureRequest)
                    .where(ErasureRequest.id == request_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if current is not None:
                original_summary = dict(current.deleted_data_summary or {})
                ran = isinstance(original_summary.get("postgres"), dict)
                pseudonym = self._pseudonym(current.user_id)
                closed = await self._close_request_without_user(
                    current, user_row_exists=ran, claimed_at=claimed_at
                )
                if closed == "interrupted":
                    try:
                        await self._resync_interrupted_payloads(
                            request_id, pseudonym, original_summary
                        )
                    except Exception as exc:
                        await self.db.rollback()
                        logger.error(
                            "erasure_sweep_payload_resync_failed",
                            request_id=str(request_id),
                            error_class=type(exc).__name__,
                        )
            logger.warning("erasure_failure_handler_stale_run", request_id=str(request_id))
            return
        except ErasureRetryScheduledError as exc:
            # #2282: nothing was erased in Postgres; the request goes back to
            # the sweep's queue. The summary is the retry block alone (no
            # ``postgres`` key can be on the row before that step's commit,
            # and the re-run writes every other key again; #2418: also after
            # a lock conflict at steps 2-3). Guarded by ``status =
            # in_progress`` like the sweep's close, and by this run's
            # ``started_at``: a row moved meanwhile,
            # or claimed again by a later sweep (this run outlived the stale
            # cutoff), is not overwritten.
            await self.db.rollback()
            result = cast(
                CursorResult[Any],
                await self.db.execute(
                    update(ErasureRequest)
                    .where(
                        ErasureRequest.id == request_id,
                        ErasureRequest.status == STATUS_IN_PROGRESS,
                        ErasureRequest.started_at == claimed_at,
                    )
                    .values(
                        status=STATUS_COOLING_OFF,
                        scheduled_for=exc.scheduled_for,
                        deleted_data_summary={_RETRY_KEY: summary[_RETRY_KEY]},
                    )
                ),
            )
            await self.db.commit()
            if not result.rowcount:
                # nothing was rescheduled (another run or a closer owns the
                # row now): the caller must not be told it will run again —
                # it gets the vector store's own error instead
                logger.warning("erasure_retry_request_moved", request_id=str(request_id))
                original = exc.__cause__
                # invariant: step 1 raises this error ``from`` the QdrantError,
                # steps 2-3 ``from`` the lock conflict (#2418, a 409), so a
                # moved row never falls through to the 503 below (which would
                # promise a retry nobody scheduled)
                if not isinstance(original, QdrantError | _LockConflict):
                    raise RuntimeError(
                        "ErasureRetryScheduledError without a QdrantError or lock-conflict cause"
                    ) from exc
                # its own cause stays as it was (the client's error)
                raise original from original.__cause__
            else:
                logger.warning(
                    "erasure_retry_scheduled",
                    request_id=str(request_id),
                    attempt=exc.attempt,
                    max_attempts=exc.max_attempts,
                    scheduled_for=to_utc_iso(exc.scheduled_for),
                    error_class=exc.error_class,
                )
            raise
        except BillingContractActiveError as exc:
            # Caller-actionable, like the transfer case below: terminal
            # `failed` with a structured reason; the user creates a new request
            # once the contract has ended. This run deleted nothing (the check
            # runs before step 1); an earlier attempt's step 1 may have (#2282).
            await self.db.rollback()
            result = cast(
                CursorResult[Any],
                await self.db.execute(
                    update(ErasureRequest)
                    # #2306: this run's row only (see the generic handler)
                    .where(
                        ErasureRequest.id == request_id,
                        ErasureRequest.started_at == claimed_at,
                        ErasureRequest.status == STATUS_IN_PROGRESS,
                    )
                    .values(
                        status=STATUS_FAILED,
                        failure_reason=(
                            f"billing_contract_active: workspace_ids={','.join(exc.details['workspace_ids'])}. "
                            "Cancel the subscription and wait for the contract period to end, "
                            "then create a new erasure request."
                        )[:1000],
                        deleted_data_summary=summary or None,
                    )
                ),
            )
            await self.db.commit()
            if not result.rowcount:
                logger.warning("erasure_failure_handler_stale_run", request_id=str(request_id))
            elif points_may_be_gone():
                # #2358: a retry run — the account stays, an earlier
                # attempt may have deleted the subject's points
                await self._requeue_after_points_deleted(
                    request_id, subject_id, _requeue_block(summary), status=STATUS_FAILED
                )
            raise
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
            result = cast(
                CursorResult[Any],
                await self.db.execute(
                    update(ErasureRequest)
                    # #2306: this run's row only (see the generic handler)
                    .where(
                        ErasureRequest.id == request_id,
                        ErasureRequest.started_at == claimed_at,
                        ErasureRequest.status == STATUS_IN_PROGRESS,
                    )
                    .values(
                        status=STATUS_FAILED,
                        failure_reason=(
                            f"workspace_transfer_required: workspace_id={exc.details.get('workspace_id')} "
                            f"member_count={exc.details.get('member_count')}. "
                            "Promote another member to admin (or remove members), then create a new erasure request."
                        )[:1000],
                        deleted_data_summary=summary or None,
                    )
                ),
            )
            await self.db.commit()
            if not result.rowcount:
                logger.warning("erasure_failure_handler_stale_run", request_id=str(request_id))
            elif points_may_be_gone():
                # #2358: step 1 (this run's or an earlier attempt's) may have
                # deleted the subject's points and the account stays (the
                # rollback above undid any uncommitted ownership change):
                # re-queue their embeddings, only when this run's failure was
                # written
                await self._requeue_after_points_deleted(
                    request_id, subject_id, _requeue_block(summary), status=STATUS_FAILED
                )
            raise
        except Exception as exc:
            if erased_in_postgres:
                # #2268 review: the subject is erased — no account id, no
                # exception text (it can name the account), as on the row
                logger.error(
                    "erasure_failed_after_postgres_commit",
                    request_id=str(request_id),
                    step=step,
                    error_class=type(exc).__name__,
                )
            else:
                logger.error(
                    "erasure_execute_failed",
                    request_id=str(request_id),
                    user_id=target.user_id,
                    error=str(exc),
                    exc_info=True,
                )
            await self.db.rollback()
            # Use a fresh write to record the failure without depending on
            # the just-rolled-back ORM state.
            # #2282: the retry budget is spent (a retryable failure with
            # attempts left raised ErasureRetryScheduledError above); nothing
            # was erased in Postgres, a new request is the retry
            failure_reason = vector_store_gave_up or retry_gave_up or str(exc)
            values: dict[str, Any] = {
                "status": STATUS_FAILED,
                "failure_reason": failure_reason[:1000],
                # ``summary`` carries the earlier attempts' ``retry`` block
                # (#2282), so a failed row keeps its history too
                "deleted_data_summary": summary or None,
            }
            if erased_in_postgres:
                # #2268: the subject is erased in Postgres and the user row is
                # gone, so nobody can retry: the row must not keep the raw sub
                # any more than a ``complete`` one does (``_finalize``), and
                # the marker lets the re-run and later erasures find it.
                values.update(
                    pseudonymized_row,
                    # the class only: an exception text (a constraint's
                    # DETAIL, a Redis key) can name the account (#2268 review)
                    failure_reason=f"{FAILURE_AFTER_POSTGRES_COMMIT}{step}: {type(exc).__name__}",
                )
            # #2306: guarded by this run's claim, like the reschedule above:
            # a run that outlived the stale cutoff and was claimed again must
            # not mark the newer run's row. Not once this run erased the
            # subject in Postgres: that fact outranks the newer claim (whose
            # subject is gone), and the row must not keep the raw sub nor
            # lose the marker the re-run and later erasures look for.
            # #2302 review: and still running — an older run of the same row
            # that outlived the cutoff may have completed it (``_finalize``
            # writes by id alone), and a ``complete`` row is the truth then
            guard = [ErasureRequest.id == request_id]
            if not erased_in_postgres:
                guard += [
                    ErasureRequest.started_at == claimed_at,
                    ErasureRequest.status == STATUS_IN_PROGRESS,
                ]
            result = cast(
                CursorResult[Any],
                await self.db.execute(update(ErasureRequest).where(*guard).values(**values)),
            )
            await self.db.commit()
            if not result.rowcount:
                logger.warning("erasure_failure_handler_stale_run", request_id=str(request_id))
            elif points_may_be_gone() and not erased_in_postgres:
                # The subject's points may be gone and the account stays:
                # the give-up after the last vector-store attempt (#2304), a
                # failure after step 1 and before the Postgres commit (#2358:
                # a non-transfer error at step 2, an error inside the Postgres
                # step), or any failure of a run after an earlier attempt
                # (#2358: e.g. a configuration error at step 1 on a retry).
                # Only when this run's failure was written (a newer run owns a
                # row the guard did not match). Never once
                # the Postgres step committed: the rows are the pseudonym's
                # and their points were deleted by design.
                await self._requeue_after_points_deleted(
                    request_id, subject_id, _requeue_block(summary), status=STATUS_FAILED
                )
            raise

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    async def _list_owned_workspaces(self, user_id: str) -> list[Workspace]:
        """All workspaces this user owns (drives the ownership-transfer / member
        checks), in id order: step 2 locks them in this order (#2418)."""
        result = await self.db.execute(
            select(Workspace).where(Workspace.owner_user_id == user_id).order_by(Workspace.id)
        )
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
            if ws.deleted_at is not None:
                continue  # soft-deleted: hard-deleted with the sole-owned ones (#2406)
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

        Lock order (#2418, ``lock_identity_links``): the subject's ``users``
        row (held since ``_guard_erasure_run``) -> the identity-link lock ->
        each workspace to hand on, in id order, ``NOWAIT``. This transaction
        holds a ``users`` row, so it never waits on a workspace row: the
        subject's own ``DELETE /workspaces/W`` holds W and then updates that
        ``users`` row. A busy workspace raises :class:`_LockConflict` and
        stops the step (``_execute`` reschedules the request); it is not
        skipped, or a shared workspace could be left to the sole-owner
        cascade of step 3.

        Raises:
            WorkspaceTransferRequiredError: Members but no other admin.
            _LockConflict: A workspace to hand on is locked by another
                transaction.
        """
        transfers: list[dict[str, str]] = []
        sole_owner_count = 0

        if not workspaces:
            return {"transferred": transfers, "sole_owner_workspaces": sole_owner_count}

        # Serialize successor selection with administrative/CLI/erasure deletion.
        # Do not acquire successor User locks after holding the subject User: the
        # shared identity lock provides this fence without reversing user order.
        # Order: users -> link lock -> live workspaces (id, NOWAIT) -> soft-deleted
        # workspaces -> contexts -> memories -> child rows (#2418).
        from services.identity_link_service import lock_identity_links

        await lock_identity_links(self.db)
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

        for ws in sorted(workspaces, key=lambda w: w.id):
            if ws.deleted_at is not None:
                # Its owner deleted it: never handed on, hard-deleted with the
                # sole-owned ones (#2406).
                continue
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
            # #1102: take the SHARED workspace-row lock so this owner-mutation
            # serializes with transfer_ownership / update_member_role on the same
            # FOR UPDATE row — the workspaces were loaded by an UNLOCKED SELECT
            # (_list_owned_workspaces), so without this the epoch read-modify-write
            # below could race a concurrent transfer and lose an increment.
            # #2418: NOWAIT — this transaction holds the subject's users row.
            try:
                locked_ws = await lock_workspace_for_update(self.db, ws.id, nowait=True)
            except ConflictError as exc:
                # Stop the step, never ``continue``: a skipped shared
                # workspace would still be owned by the subject at step 3.
                raise _LockConflict(LOCK_CONFLICT_ERROR_CLASS) from exc
            except NotFoundException:
                # The workspace was soft-deleted concurrently (it will be cascade-
                # cleaned anyway) — nothing to auto-transfer. A benign race must NOT
                # fail the whole GDPR erasure request.
                continue
            # Re-read under the lock — the helper uses populate_existing, so this
            # reflects committed concurrent changes even though the row was already
            # in the session. If a concurrent transfer already moved ownership away
            # from the erased user, this is no longer ours to auto-transfer.
            if locked_ws.owner_user_id != user_id:
                continue
            locked_ws.owner_user_id = new_owner.user_id
            new_owner.role = WorkspaceRole.OWNER
            # Bump the ownership epoch on every owner change (#1102) so the #1100
            # consumer invalidates external credentials (billing-handoff tokens /
            # sessions) bound to the erased previous owner.
            locked_ws.ownership_epoch = locked_ws.ownership_epoch + 1
            transfers.append({"workspace_id": str(ws.id), "new_owner_user_id": new_owner.user_id})

        # Commit so the cascade on workspace delete (step 4) sees the new
        # owner_user_id rather than the about-to-be-deleted user_id.
        if transfers:
            await self.db.commit()

        return {"transferred": transfers, "sole_owner_workspaces": sole_owner_count}

    async def _lock_owned_workspaces(self, user_id: str) -> None:
        """Lock the workspaces ``user_id`` owns in id order (#2418): the live
        ones ``NOWAIT``, then the soft-deleted ones (waiting).

        Raises:
            _LockConflict: A live one is locked by another transaction.
        """
        try:
            await self.db.execute(
                select(Workspace.id)
                .where(Workspace.owner_user_id == user_id, Workspace.deleted_at.is_(None))
                .order_by(Workspace.id)
                .with_for_update(nowait=True)
            )
        except DBAPIError as exc:
            if _sqlstate(exc) == _PG_LOCK_NOT_AVAILABLE:
                raise _LockConflict(LOCK_CONFLICT_ERROR_CLASS) from exc
            raise
        await self.db.execute(
            select(Workspace.id)
            .where(Workspace.owner_user_id == user_id, Workspace.deleted_at.is_not(None))
            .order_by(Workspace.id)
            .with_for_update()
        )

    async def _guard_erasure_run(
        self, target: User, *, request_id: UUID | None, lock_request: bool = False
    ) -> None:
        """#2362: keep users first and requests last in the erasure lock order.

        A normal read under the user lock is enough before external I/O: the
        original erasure cannot commit its user deletion while we hold that
        row. The final check additionally locks the request after all child
        rows, so an old marker can never be overwritten by a reclaimed run.
        """
        present = (
            await self.db.execute(select(User.id).where(User.id == target.id).with_for_update())
        ).scalar_one_or_none()
        if request_id is not None:
            statement = select(ErasureRequest.deleted_data_summary).where(
                ErasureRequest.id == request_id
            )
            if lock_request:
                statement = statement.with_for_update()
            summary = (await self.db.execute(statement)).scalar_one_or_none()
            if isinstance(summary, dict) and isinstance(summary.get("postgres"), dict):
                raise _StaleErasureRun()
        if present is None:
            raise _StaleErasureRun()

    async def _delete_postgres(
        self, target: User, *, request_id: UUID | None = None
    ) -> PostgresErasure:
        """Delete every Postgres row that references the user, and commit.

        Order matters because not every column is FK-cascaded — most
        cross-table user references are plain VARCHAR (OAuth2 sub) with
        no cascade. The full sweep here is the application-layer
        equivalent of `ON DELETE CASCADE` for all per-user data. The one
        transfer: the subject's live connector keys go to their workspace's
        owner before the per-user ``api_keys`` delete (#2305).

        Args:
            target: The user being erased.
            request_id: The erasure request (``_execute`` passes it, #2286):
                the ``postgres`` summary (counts, ``memory_scrub_at``) is
                written on that row in this transaction, so the fact that
                this step committed survives a crash before any later step
                records it (``_close_request_without_user``). None writes
                nothing on a request row.

        Returns:
            The counts, and the details restore whose payloads the caller
            syncs after this commit (``sync_restored_payloads``).
        """
        # Optional model imports so the service doesn't crash if a model
        # is renamed/removed in a future refactor.
        from models.auth import ContextReadAttribution, PlanChange, UsageStats
        from models.memory import GraphMemory

        user_id = target.user_id
        counts: dict[str, int] = {}
        # Ownership transfers can commit, releasing the pre-vector user lock.
        await self._guard_erasure_run(target, request_id=request_id)

        # #2347: the sole-owner cascade also reaches contexts/memories. Take
        # the identity lock and one ordered union BEFORE any cascade, not just
        # before the later authorship sweep. Admin deletion uses this order.
        from services.identity_link_service import lock_identity_links

        await lock_identity_links(self.db)
        # #2418: the workspaces the subject owns, before the context union —
        # the order admin deletion takes (``delete_user_rows``). The live ones
        # NOWAIT: this transaction holds the subject's users row, and the
        # subject's own DELETE /workspaces/W holds W and then updates it. The
        # soft-deleted ones wait: no owner mutation or workspace delete takes
        # them (``lock_workspace_for_update`` locks live rows only), and the
        # deleted-workspace file repair locks them SKIP LOCKED holding no user.
        await self._lock_owned_workspaces(user_id)
        owned_ws = select(Workspace.id).where(Workspace.owner_user_id == user_id)
        affected_contexts = select(Context.id).where(
            or_(
                Context.created_by == user_id,
                Context.deleted_by == user_id,
                Context.workspace_id.in_(owned_ws),
            )
        )
        await self.db.execute(
            locked_ids(
                Context,
                lambda c: (
                    or_(
                        c.created_by == user_id,
                        c.deleted_by == user_id,
                        c.workspace_id.in_(owned_ws),
                    ),
                ),
            )
        )
        await self.db.execute(
            locked_ids(
                Memory,
                lambda m: (
                    or_(
                        m.id.in_(subject_memory_ids(user_id)),
                        m.deleted_by == user_id,
                        m.context_id.in_(affected_contexts),
                    ),
                ),
            )
        )

        # Remove the OAuth grants first, through the one revoker that owns
        # the lock order (users → codes → device codes → tokens, #1770): it
        # takes the user row FOR UPDATE, which every grant writer share-locks
        # before it writes (#1738). A consent, device approval or refresh
        # racing this sweep either lands before it and is removed here, or
        # waits on the row and — since this transaction deletes the row
        # below — then finds no user to grant for and writes nothing. This is
        # an erasure, so the tokens are deleted rather than revoked.
        revoked = await revoke_oauth_grants(self.db, user_id, delete_tokens=True)
        counts["oauth_authorization_codes"] = revoked.authorization_codes
        counts["oauth_tokens"] = revoked.tokens

        # OAuth2 clients — no FK cascade from users to these.
        counts["oauth_clients"] = await self._count_and_delete(
            OAuth2Client, OAuth2Client.owner_id == user_id
        )
        # #1365 review sweep: device-authorization-flow rows cascade on client
        # deletion only, so the subject's raw sub in user_id survives an
        # OAuth-token erasure. Transient per-user rows — delete the subject's.
        counts["oauth_device_codes"] = revoked.device_codes
        # #2129: per-grant settings (the app's default context) — keyed by the
        # raw sub, no FK to users.
        counts["oauth_grant_settings"] = await self._count_and_delete(
            OAuthGrantSetting, OAuthGrantSetting.user_id == user_id
        )

        # Direct per-user tables.
        # #1769: the FK cascades too; deleted and counted here so the erasure
        # summary lists them (the password reset deletes the same rows with
        # ``known_devices_delete``, which does not count them).
        counts["user_known_devices"] = await self._count_and_delete(
            UserKnownDevice, UserKnownDevice.user_id == user_id
        )
        counts["external_api_keys"] = await self._count_and_delete(
            ExternalAPIKey, ExternalAPIKey.user_id == user_id
        )
        # #2305: a connector's write key the subject provisioned serves the
        # workspace, not the person — after step 2 the owner is never the
        # subject (a sole-owned workspace goes with its keys below), so hand
        # the live keys to the owner; the revoked ones are deleted with the rest.
        counts["connector_api_keys_reowned"] = await self._reown_connector_keys(user_id)
        counts["api_keys"] = await self._count_and_delete(APIKey, APIKey.user_id == user_id)
        counts["graph_memory"] = await self._count_and_delete(
            GraphMemory, GraphMemory.user_id == user_id
        )
        counts["usage_stats"] = await self._count_and_delete(
            UsageStats, UsageStats.user_id == user_id
        )
        # #1365 review sweep: LLM call/cost telemetry has NO FK cascade at all,
        # so the caller's raw sub in user_id would outlive erasure entirely.
        # Per-user telemetry — delete the subject's rows (mirrors usage_stats).
        from models.llm_call_log import LLMCallLog

        counts["llm_call_logs"] = await self._count_and_delete(
            LLMCallLog, LLMCallLog.user_id == user_id
        )
        # #1228: diagnostic cross-context read attributions are per-user
        # data too (context_id FK cascades on context deletion, but the
        # user sweep must not rely on that).
        counts["context_read_attributions"] = await self._count_and_delete(
            ContextReadAttribution, ContextReadAttribution.user_id == user_id
        )

        # Workspace memberships and invitations.
        counts["workspace_members"] = await self._count_and_delete(
            WorkspaceMember, WorkspaceMember.user_id == user_id
        )
        counts["workspace_invitations"] = await self._count_and_delete(
            WorkspaceInvitation,
            (WorkspaceInvitation.invited_by == user_id)
            | (WorkspaceInvitation.email == target.email),
        )
        # #1365 review sweep (F8): per-context ACL grants. The subject's own
        # memberships are dead after erasure — delete them (mirrors
        # workspace_members). ``invited_by`` on OTHERS' surviving rows, and the
        # surviving-row ``WorkspaceMember.invited_by`` / ``WorkspaceInvitation
        # .accepted_by`` links (F9), are pseudonymized in the authorship sweep.
        counts["context_members"] = await self._count_and_delete(
            ContextMember, ContextMember.user_id == user_id
        )

        # Pseudonymize plan_changes.changed_by — keep the audit trail but
        # break the link to the deleted user (legal retention concern).
        counts["plan_changes_pseudonymized"] = await self._pseudonymize_field(
            PlanChange, PlanChange.changed_by, user_id
        )

        # Capture before cascade: UUID-only scopes survive deleted rows and
        # a post-commit vector outage. Insert commits with the erasure itself.
        if request_id is not None:
            from services.vector_erasure import capture_vector_erasure

            counts["vector_erasure_scopes"] = await capture_vector_erasure(
                self.db, request_id, user_id
            )

        # Workspaces owned by the user. After step 3 ran, only sole-owner
        # workspaces remain pointing here. The cascade chain wipes
        # contexts/memories/edges/etc.
        counts["workspaces"] = await self._count_and_delete(
            Workspace, Workspace.owner_user_id == user_id
        )

        # Pseudonymize agents.owner_user_id (#1274, RFC-0002 P0-1) AFTER the
        # workspace cascade so the count reflects only survivors — agents in
        # co-owned (transferred) workspaces whose rows outlive the erased
        # subject. Same legal-retention posture as plan_changes.changed_by.
        counts["agents_pseudonymized"] = await self._pseudonymize_field(
            Agent, Agent.owner_user_id, user_id
        )

        # Same for agent_context_bindings.created_by (#1275, RFC-0002 P0-2):
        # binding rows cascade with their agent/context, but bindings authored
        # by the erased subject on surviving agents must drop the personal link.
        counts["agent_bindings_pseudonymized"] = await self._pseudonymize_field(
            AgentContextBinding, AgentContextBinding.created_by, user_id
        )

        # #1358: worker app identities are GLOBAL control-plane rows — no
        # workspace FK, so no cascade ever removes them and the operator's
        # raw OAuth sub in created_by/updated_by would outlive erasure.
        # Same legal-retention posture (row survives, personal link breaks).
        # The count is per swept column: a row where the subject is both
        # creator and last updater contributes 2.
        from models.worker_app import WorkerAppIdentity

        counts["worker_app_identities_pseudonymized"] = await self._pseudonymize_field(
            WorkerAppIdentity, WorkerAppIdentity.created_by, user_id
        ) + await self._pseudonymize_field(WorkerAppIdentity, WorkerAppIdentity.updated_by, user_id)

        # #1365 (1): config_overrides are the same #1358 shape — GLOBAL
        # control-plane rows with no workspace FK, so no cascade ever
        # removes them and an erased admin's raw sub would persist in
        # updated_by. Same legal-retention posture.
        from models.config import ConfigOverride

        counts["config_overrides_pseudonymized"] = await self._pseudonymize_field(
            ConfigOverride, ConfigOverride.updated_by, user_id
        )

        # #1365 (3+4): workspace-scoped authorship columns on rows that
        # OUTLIVE the erased subject (co-owned/transferred workspaces —
        # the sole-owner cascade above already wiped the rest). Same
        # posture as agent_context_bindings.created_by (#1275): row
        # survives, personal link breaks. contexts.created_by closes the
        # #1336 asymmetry (deleted_by was NULL-cleared there; created_by
        # was the leftover).
        from models.file_objects import FileObject
        from models.resource import (
            Resource,
            ResourceToken,
            WorkspaceAddon,
            WorkspaceConnector,
        )
        from models.secrets import (
            RecipientPubkey,
            Secret,
            SecretGrant,
            SecretVersion,
        )

        # #1784: a private context the subject created may hold memories a
        # linked account wrote. Pass it to that account before created_by is
        # pseudonymized below, or those memories go out of anyone's reach.
        from services.identity_link_service import (
            hand_over_private_contexts,
            lock_identity_links,
        )

        # #2278: one lock phase over every context this erasure writes — the
        # ones the subject created (hand-over, authorship sweep) and the ones
        # it deleted (cleared with the memory steps) — in id order, before the
        # hand-over's memory DELETE: contexts before memories, the order
        # ``restore_deleted_context`` takes. Two separately ordered phases
        # (created_by, then deleted_by) would let two erasures each hold a
        # context the other deleted. The link lock first, the order the
        # hand-over and admin user delete take (users row, link lock,
        # contexts); the hand-over re-takes it for free.
        await lock_identity_links(self.db)
        await self.db.execute(
            locked_ids(Context, lambda c: (or_(c.created_by == user_id, c.deleted_by == user_id),))
        )
        counts.update(await hand_over_private_contexts(self.db, user_id))

        authorship_sweeps: tuple[tuple[Any, Any], ...] = (
            (WorkspaceConnector, WorkspaceConnector.created_by),
            (Resource, Resource.created_by),
            (ResourceToken, ResourceToken.created_by),
            (WorkspaceAddon, WorkspaceAddon.created_by),
            (FileObject, FileObject.created_by),
            (Secret, Secret.created_by),
            (SecretVersion, SecretVersion.created_by),
            (RecipientPubkey, RecipientPubkey.created_by),
            # attested_by: another user's sub lands here when they attest a
            # pubkey — same surviving-row shape (code-review finder, #1365).
            (RecipientPubkey, RecipientPubkey.attested_by),
            # identity_id: the OWNER of the recipient key — the primary subject
            # column of the recipient-key table, missed by the created_by/
            # attested_by sweep though it holds the same raw sub (F7).
            (RecipientPubkey, RecipientPubkey.identity_id),
            (SecretGrant, SecretGrant.granted_by),
            (Context, Context.created_by),
            # updated_by on keys OWNED BY OTHERS survives the user_id delete
            # above (code-review finder, #1365).
            (ExternalAPIKey, ExternalAPIKey.updated_by),
            # F8/F9 surviving-row authorship: invited_by on OTHERS' context/
            # workspace memberships, and accepted_by on invitations a DIFFERENT
            # user created but the erased subject accepted, all outlive the
            # user_id/invited_by deletes above with the raw sub intact.
            (ContextMember, ContextMember.invited_by),
            (WorkspaceMember, WorkspaceMember.invited_by),
            (WorkspaceInvitation, WorkspaceInvitation.accepted_by),
        )
        authorship_total = 0
        for model, column in authorship_sweeps:
            authorship_total += await self._pseudonymize_field(model, column, user_id)
        counts["authorship_columns_pseudonymized"] = authorship_total

        # #1365 (2): secret_access_log — append-only, HMAC hash-chained
        # audit rows that deliberately survive entity deletion. The e72
        # trigger carve-out permits UPDATE of ONLY the identity columns.
        counts["secret_access_log_pseudonymized"] = await self._erase_secret_access_log(user_id)

        # #1336 Gap 2: author memories that OUTLIVE the erased subject — rows
        # in co-owned/transferred workspaces (the sole-owner cascade above
        # already wiped the rest). Their Qdrant points were deleted in step 1
        # but the PG rows kept details (coordinates included) and the raw
        # user_id. Pseudonymize the subject link and NULL the details column (PostgreSQL json) —
        # which also NULLs the generated columns (location_lat/lon) derived
        # from it; a time memory keeps only its trigger (#2266). Runs AFTER the workspace
        # cascade so the count reflects only survivors.
        # #2043: the revision log of surviving memories. BEFORE the memory
        # scrub: it still finds the subject's memories by their raw user_id.
        # The subject's tombstones are hard-deleted before it (#2263) and
        # take their revisions with them through the FK cascade.
        # #2044: BEFORE the revision scrub, which NULLs the subject's session
        # ids that this one matches memories' last-writer sessions against.
        # #2242: lock the subject's memories in id order first, before any of
        # the memories / memory_revisions UPDATEs below, so edits, session
        # rollbacks and other erasures queue on them instead of deadlocking.
        # #2263: in the same ordered statement as the subject's memories, the
        # memories the subject deleted, cleared at the end of the memory
        # steps. The contexts it deleted are held since the hand-over (#2278):
        # one lock phase per table, contexts before memories.
        await lock_subject_memories(self.db, user_id, deleted_by=True)
        if request_id is not None:
            # Include ingests committed after the pre-cascade snapshot.
            await capture_vector_erasure(self.db, request_id, user_id)
        # #2268: from here on no edit lands on the subject's memories until
        # the commit — every revision row this erasure sees is older
        memory_scrub_at = utcnow()
        # #2263: the subject's tombstones go before any revision statement, so
        # the details strip below never locks, rewrites or counts rows the FK
        # cascade removes with them.
        counts["memory_tombstones_deleted"] = await self._delete_subject_tombstones(user_id)
        # #2244: where the subject wrote live details keys of a memory another
        # member authored, put each back to its value from before their write
        # (#2275: per key, also where a later member's whole-dict edit carried
        # it unchanged; a key a later member rewrote stays). FIRST
        # among the memory steps: it finds the subject's edits by the raw
        # actor (pseudonymized below) and writes its own revision row under
        # the pseudonym, which the strip and the pseudonymize step never
        # match. No vector-store call here (#2266): ``_execute`` patches the
        # payloads after the commit below.
        # #2243, #2268: the restore walks past every edit of a member erased
        # earlier, even where an erasure from before the #2235 fix left their
        # value in a prior. The strip runs right after it, from the same read
        # of the logs (``erase_subject_details``).
        details = await erase_subject_details(
            self.db,
            user_id,
            actor=self._pseudonym(user_id),
            erased_actors=await erased_pseudonyms(self.db),
        )
        restore = details.restore
        counts["details_restored"] = restore.restored
        counts["details_restored_unavailable"] = restore.unavailable
        # #2275: per key — the subject's live keys put back or removed, and
        # the live keys kept because a state an erasure before v0.129.0
        # stripped whole may hold their value
        counts["details_keys_reverted"] = restore.keys_reverted
        counts["details_keys_ambiguous"] = restore.keys_ambiguous
        counts["details_embedding_requeued"] = restore.embedding_requeued
        counts.update(await self._erase_session_provenance(user_id))
        counts.update(await self._erase_memory_revisions(user_id, details.strip))
        # #2363 / #2393: every survivor (a ``success`` row too) leaves the
        # embedding queue before the scrub, which takes the raw sub off them:
        # step 1 deleted the subject's points, and the sweep must not write
        # them again under the pseudonym. The count equals the survivors.
        counts["memories_embedding_dequeued"] = await self._dequeue_surviving_embeddings(user_id)
        counts["memories_scrubbed"] = await self._scrub_surviving_memories(user_id)

        # #1365 review sweep: neural graph edges, analysis runs, retrieval
        # feedback, and sleep reports for the erased subject that OUTLIVE the
        # sole-owner cascade (co-owned/transferred workspaces — sole-owner rows
        # were already cascade-deleted with their contexts). Pseudonymize the
        # subject link with the SAME deterministic pseudonym as
        # _scrub_surviving_memories so the surviving graph stays internally
        # consistent (edges/analyses still line up with their pseudonymized
        # memories) while the raw sub is gone. Runs AFTER the workspace cascade
        # so counts reflect only survivors.
        from models.analysis import MemoryAnalysis
        from models.memory import NeuralMemoryEdge
        from models.retrieval_feedback import RetrievalFeedback
        from models.sleep import SleepReport

        counts["neural_edges_pseudonymized"] = await self._pseudonymize_field(
            NeuralMemoryEdge, NeuralMemoryEdge.user_id, user_id
        )
        counts["memory_analyses_pseudonymized"] = await self._pseudonymize_field(
            MemoryAnalysis, MemoryAnalysis.triggered_by, user_id
        )
        counts["retrieval_feedback_pseudonymized"] = await self._pseudonymize_field(
            RetrievalFeedback, RetrievalFeedback.user_id, user_id
        )
        counts["sleep_reports_pseudonymized"] = await self._pseudonymize_field(
            SleepReport, SleepReport.user_id, user_id
        )

        # memory_access_events (#1278, RFC-0002 P0-5): no-FK append-only audit
        # rows survive entity deletion, so the subject's rows must be
        # pseudonymized + scrubbed here. The append-only trigger permits UPDATE
        # ONLY of the carve-out columns (user_id, session_id, run_id,
        # event_metadata) — this single statement touches exactly those.
        counts["memory_access_events_pseudonymized"] = await self._erase_memory_access_events(
            user_id
        )

        # #2287: the subject's audit rows, inside this transaction — a failure
        # rolls the whole erasure back with it (the user row stays, the request
        # keeps its ids and can be retried) instead of leaving the raw sub and
        # email on the rows after the commit. Last before the user delete, so
        # nothing this transaction writes to audit_logs could be missed
        # (nothing does today). #2298: other actors' rows that name the
        # subject by an exact value (``user_email``, ``resource =
        # 'user:'||email``) are rewritten in the same step, under one
        # id-ordered lock over both sets; every other writer only inserts.
        # #2359: each address the subject held, only while they held it.
        counts.update(await self._pseudonymize_audit_logs(target))
        counts["users"] = 1  # deleted below, in this transaction

        # #2286: "this step committed", on the request row and in the same
        # commit: a crash after it (no handler runs) is then told apart from
        # a run that never reached it. The request row is the last row this
        # transaction locks (the user row is locked since the grant revoke);
        # no other writer holds a request row while waiting on these rows.
        # ``_execute`` overwrites the summary later with the fuller one.
        # Merged into what the row holds (``||``), not replacing it: a key
        # written before this step (#2282's ``retry``) survives a crash too.
        if request_id is not None:
            await self._guard_erasure_run(target, request_id=request_id, lock_request=True)
            postgres = {**counts, "memory_scrub_at": to_utc_iso(memory_scrub_at)}
            await self.db.execute(
                update(ErasureRequest)
                .where(ErasureRequest.id == request_id)
                .values(deleted_data_summary=_merged_summary({"postgres": postgres}))
                # a SQL expression: no in-session sync (it would expire the
                # loaded request's summary for an async lazy load)
                .execution_options(synchronize_session=False)
            )

        # Finally the user row itself.
        await self.db.delete(target)
        await self.db.commit()

        return PostgresErasure(counts, restore, memory_scrub_at)

    def _pseudonym(self, value: str) -> str:
        """Salted-SHA256 pseudonym: shorthand for ``erasure_pseudonym``.

        ``erasure_pseudonym`` is the one derivation (the request-row values and
        the audit-row hashes call it directly, #2288), so a future salt
        rotation / algorithm change cannot produce inconsistent pseudonyms for
        the same subject across tables (same-user cross-row correlation is the
        legal-retention contract). Patch the salt, not this method.
        """
        return erasure_pseudonym(value)

    async def _reown_connector_keys(self, user_id: str) -> int:
        """Hand the subject's live connector write keys to their workspace's
        owner (#2305) and return how many moved (summary key
        ``connector_api_keys_reowned``).

        The contract — scope, the ``owner_user_id <> subject`` guard and the
        lock order — lives in ``services.connector_key_ownership``, shared with
        member removal, the admin user delete and ``cli.delete_admin`` (#2352).
        """
        return await reown_connector_keys(self.db, user_id)

    async def _count_and_delete(self, model: Any, where_clause: Any) -> int:
        """Delete rows and return the affected count from the cursor.

        Single round-trip: PostgreSQL returns ``rowcount`` from ``DELETE``
        directly, so we don't need a separate ``SELECT count()``.
        """
        result = cast(
            CursorResult[Any],
            await self.db.execute(delete(model).where(where_clause)),
        )
        return result.rowcount or 0

    async def _erase_memory_access_events(self, user_id: str) -> int:
        """Pseudonymize + scrub the erased subject's memory_access_events rows.

        One UPDATE touching ONLY the append-only carve-out columns
        (user_id, session_id, run_id, event_metadata), so the append-only
        trigger permits it:

        - ``user_id`` → salted-SHA256 pseudonym (keeps the row's non-personal
          audit shape while breaking the personal-data link, same lane as
          plan_changes/audit_logs);
        - ``session_id`` / ``run_id`` → NULL scrubbed. These are opaque
          per-session correlation tokens with no post-erasure analytical value,
          and because a contract is not a control (a client may have embedded a
          name despite the MUST-NOT), a definitive NULL is stronger than a
          hash and needs no pgcrypto;
        - ``event_metadata`` → redacted marker (the "opaque tokens" contract is
          not trusted to keep PII out of client-controlled JSONB — the
          llm_call_log lesson).

        Rows keep their non-personal audit value.
        """
        from models.memory_access_event import MemoryAccessEvent

        pseudonym = self._pseudonym(user_id)
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                update(MemoryAccessEvent)
                .where(MemoryAccessEvent.user_id == user_id)
                .values(
                    user_id=pseudonym,
                    session_id=None,
                    run_id=None,
                    event_metadata={"redacted": "erased_subject"},
                )
            ),
        )
        return result.rowcount or 0

    async def _erase_secret_access_log(self, user_id: str) -> int:
        """Pseudonymize the erased subject in ``secret_access_log`` (#1365).

        The e72 append-only carve-out permits UPDATE of ONLY
        ``(actor_user_id, recipient_identity)`` — the #1278 pattern. Two
        UPDATEs, one per identity column; a row where the subject is
        both actor and recipient contributes 2 to the count (mirrors
        worker_app_identities' per-column sum).

        Tamper-evidence trade-off (runbook §2.4): rows are HMAC
        hash-chained, so the stored ``entry_hash`` no longer recomputes
        for exactly the mutated rows. Chain linkage (``prev_hash``
        pointers) is untouched — every other row still verifies — and
        GDPR/APPI erasure takes precedence over per-row content
        authentication for the erased subject. Verifiers must treat
        entry-hash mismatches on pseudonymized rows as expected.
        """
        from models.secrets import SecretAccessLog

        pseudonym = self._pseudonym(user_id)
        actor = cast(
            CursorResult[Any],
            await self.db.execute(
                update(SecretAccessLog)
                .where(SecretAccessLog.actor_user_id == user_id)
                .values(actor_user_id=pseudonym)
            ),
        )
        recipient = cast(
            CursorResult[Any],
            await self.db.execute(
                update(SecretAccessLog)
                .where(SecretAccessLog.recipient_identity == user_id)
                .values(recipient_identity=pseudonym)
            ),
        )
        return (actor.rowcount or 0) + (recipient.rowcount or 0)

    async def _erase_session_provenance(self, user_id: str) -> dict[str, int]:
        """Scrub the subject's session tokens outside ``memory_revisions`` (#2044).

        - ``memories.session_id`` is the last writer's session: NULLed where
          that session is one the subject wrote this memory under (the
          revision log still names the subject as the actor here). Another
          member's later session on the same memory is left alone. Driven
          from the subject's own revisions, so other tenants' rows are never
          scanned.
        - ``memory_session_rollbacks``: the subject as the session's principal
          gets the salted pseudonym and a NULL ``session_id``; as the one who
          requested a rollback, the pseudonym — and a NULL ``session_id`` on
          a context owner's rollback (no principal), whose id may be the
          subject's own session.

        Returns:
            ``memory_sessions_scrubbed`` and ``session_rollbacks_pseudonymized``
            counts for the erasure summary.
        """
        from models.memory import MemorySessionRollback

        subject_sessions = select(MemoryRevision.memory_id, MemoryRevision.session_id).where(
            MemoryRevision.actor_user_id == user_id,
            MemoryRevision.session_id.is_not(None),
        )
        # #2263: in id order, like every multi-row write over ``memories`` here
        # (these rows are the subject's, already held: the re-lock is free)
        memories = cast(
            CursorResult[Any],
            await self.db.execute(
                update(Memory)
                .where(
                    Memory.id.in_(
                        locked_ids(
                            Memory, lambda m: (tuple_(m.id, m.session_id).in_(subject_sessions),)
                        )
                    )
                )
                .values(session_id=None)
                .execution_options(synchronize_session=False)
            ),
        )
        pseudonym = self._pseudonym(user_id)
        principal = cast(
            CursorResult[Any],
            await self.db.execute(
                update(MemorySessionRollback)
                .where(MemorySessionRollback.principal_user_id == user_id)
                .values(principal_user_id=pseudonym, session_id=None)
            ),
        )
        requester = cast(
            CursorResult[Any],
            await self.db.execute(
                update(MemorySessionRollback)
                .where(MemorySessionRollback.requested_by == user_id)
                .values(
                    requested_by=pseudonym,
                    session_id=case(
                        (MemorySessionRollback.principal_user_id.is_(None), None),
                        else_=MemorySessionRollback.session_id,
                    ),
                )
            ),
        )
        return {
            "memory_sessions_scrubbed": memories.rowcount or 0,
            "session_rollbacks_pseudonymized": (principal.rowcount or 0)
            + (requester.rowcount or 0),
        }

    async def _erase_memory_revisions(
        self, user_id: str, stripped: StripCounts | None = None
    ) -> dict[str, int]:
        """Scrub the erased subject out of ``memory_revisions`` (#2043).

        Two steps of ``services.erasure_details``, in this order:
        ``strip_subject_details`` (the subject's ``details`` keys leave every
        prior that holds them, per key, #2275; the authored memories' priors
        lose ``details`` whole and their events are marked scrubbed — the
        revision-log twin of ``_scrub_surviving_memories``, which NULLs the
        surviving memories' ``details``), then
        ``pseudonymize_revision_actor`` (the subject's own events get the
        salted pseudonym as actor and a NULL session). The strip finds the
        subject's edits by the raw ``actor_user_id``, so it goes first.

        Runs BEFORE ``_scrub_surviving_memories``: the strip finds the
        authored memories by the raw ``user_id``. Each statement is scoped
        to the subject's memories and locks its rows in id order (#2242).

        ``stripped``: the strip's counts when the caller ran it already
        (``_delete_postgres`` runs it in ``erase_subject_details``, right
        after the restore, #2268); None runs it here.

        Returns:
            ``memory_revisions_pseudonymized`` (the subject's own events) and
            ``revision_details_stripped`` (priors whose ``details`` was
            removed) for the erasure summary.
        """
        if stripped is None:
            stripped = await strip_subject_details(self.db, user_id)
        pseudonymized = await pseudonymize_revision_actor(
            self.db, user_id, self._pseudonym(user_id)
        )
        logger.debug(
            "erasure_revisions_scrubbed",
            details_stripped=stripped.details_stripped,
            scrub_marked=stripped.scrub_marked,
        )
        return {
            "memory_revisions_pseudonymized": pseudonymized,
            "revision_details_stripped": stripped.details_stripped,
        }

    async def _delete_subject_tombstones(self, user_id: str) -> int:
        """Hard-delete the erased subject's tombstoned memories (#1336, #2263).

        They are invisible to every member already and their Qdrant points
        were removed in step 1 (``delete_user_points``), so a hard DELETE
        beats scrubbing — a pseudonymized tombstone would be unpurgeable
        forever (no sleep run ever executes for the pseudonym, so no
        retention window matches it). Their revisions go with them (FK
        cascade). Runs right after ``lock_subject_memories``, before any
        revision statement, so the details strip never locks, rewrites or
        counts rows about to be deleted. The rows are the subject's, already
        held; the id-ordered lock keeps every multi-row write here in one
        order.

        Returns:
            How many tombstones were deleted.
        """
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(Memory)
                .where(
                    Memory.id.in_(
                        locked_ids(
                            Memory, lambda m: (m.user_id == user_id, m.deleted_at.is_not(None))
                        )
                    )
                )
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount or 0

    async def _delete_late_points(self, user_id: str, request_id: UUID | None) -> dict[str, Any]:
        """Drain captured UUID scopes after the Postgres erasure commits.

        Vector failures are recorded as pending jobs and retried regardless
        of request status. The summary is the attempt's ``{completed,pending}``
        counts. Database failures propagate to the existing after-commit
        failure handler; no raw identity is needed for the eventual retry.
        """
        from services.vector_erasure import drain_vector_erasures

        if request_id is None:
            # Internal callers without a lifecycle row have no durable retry.
            return {"qdrant_after_commit": await delete_user_points(user_id)}
        return {"qdrant_after_commit": await drain_vector_erasures(self.db, request_id=request_id)}

    async def _dequeue_surviving_embeddings(self, user_id: str) -> int:
        """Take the subject's surviving memories out of the embedding queue (#2363).

        Step 1 deleted every point of the subject. Every surviving memory,
        including a ``success`` row, becomes terminal: ``failed`` with
        ``ERASED_AUTHOR_EMBEDDING_ERROR``, the retry
        budget spent (``MAX_EMBEDDING_RETRIES``, so
        ``embedding_retry_eligible_clause`` never matches it; the reclaim
        clauses need ``processing``) and the pipeline's clock stamped. A
        ``success`` row must also be terminal (#2393): an older run that lost
        its claim can still write after a newer run succeeded and erasure
        finished; leaving ``success`` would let its lost-claim repair requeue
        the erased author's row automatically. A point a run wrote before the
        commit goes with ``_delete_late_points``. No automatic retry takes
        these rows again; the admin retry-failed
        endpoint with ``include_erased=true`` explicitly brings them back, and
        they then embed under the pseudonym with no ``details``. Model switches
        and every automatic writer exclude the durable suppression marker; a context merge copies
        them still terminal (``ContextService.merge_contexts``). The count
        returned equals the subject's survivors (``memories_scrubbed``).

        The status change also fences a run in flight: both of its outcome
        writes match ``processing`` and its own claim stamp, so it sees its
        claim lost, and a run that wrote points by then deletes them
        (``memory_service._reconcile_after_lost_claim``: the row's author is no
        longer the one the run wrote). An earlier
        ``embedding_error`` is overwritten: the marker is what an operator
        needs to read.

        By the raw sub, before ``_scrub_surviving_memories`` pseudonymizes the
        rows; they are held since ``lock_subject_memories``, so the id-ordered
        re-lock is free. Rolled back with the Postgres step on any failure.

        Returns:
            How many rows were taken out of the queue.
        """
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                update(Memory)
                .where(
                    Memory.id.in_(
                        locked_ids(
                            Memory,
                            lambda m: (m.user_id == user_id,),
                        )
                    )
                )
                .values(
                    embedding_status="failed",
                    embedding_error=ERASED_AUTHOR_EMBEDDING_ERROR,
                    embedding_suppressed_at=utcnow(),
                    embedding_retry_count=MAX_EMBEDDING_RETRIES,
                    # the pipeline's own clock (#1852); updated_at is an edit's
                    embedding_attempted_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount or 0

    async def _scrub_surviving_memories(self, user_id: str) -> int:
        """Scrub author memories that outlive the erased subject (#1336).

        ``user_id`` becomes the salted pseudonym (the plan_changes/agents
        legal-retention convention: same-user correlation stays possible for
        compliance, the sub is unrecoverable) and ``details`` is NULLed —
        the memory text remains workspace content, but structured personal
        payload (coordinates, arbitrary detail keys) is removed at rest. The
        subject's tombstones are gone already (``_delete_subject_tombstones``),
        and all surviving authored rows are terminalized
        (``_dequeue_surviving_embeddings``, #2363).

        A ``type='time'`` memory keeps ``{"trigger": ...}`` and nothing else
        (#2266): ``valid_trigger_window_format`` requires its trigger window,
        which is scheduling metadata of the memory kept like its text. A NULL
        there failed the whole erasure on the CHECK.

        Every statement locks its rows in id order (#2263); all of them are
        held since the erasure's first memory step (the authored and the
        ``deleted_by`` memories by ``lock_subject_memories``, the contexts
        before it), so the re-lock is free. ``synchronize_session=False``:
        nothing in the session reads these rows afterwards, and the ORM
        cannot evaluate the subquery in memory (it would fetch every id back).
        """
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                update(Memory)
                .where(Memory.id.in_(locked_ids(Memory, lambda m: (m.user_id == user_id,))))
                .values(user_id=self._pseudonym(user_id), details=_trigger_only_details())
                .execution_options(synchronize_session=False)
            ),
        )
        # The subject's raw sub also survives in OTHER rows' deleted_by
        # (forget()/context-delete stamp the actor) and contexts.deleted_by.
        # Clear to NULL — the member-removal precedent
        # (member_credentials_service) — so no raw sub outlives erasure.
        for model in (Memory, Context):
            await self.db.execute(
                update(model)
                .where(model.id.in_(locked_ids(model, lambda m: (m.deleted_by == user_id,))))
                .values(deleted_by=None)
                .execution_options(synchronize_session=False)
            )
        return result.rowcount or 0

    async def _pseudonymize_field(
        self,
        model: Any,
        column: Any,
        user_id: str,
    ) -> int:
        """SHA256-pseudonymize a column across all matching rows.

        Used for legal-retention tables (e.g. plan_changes) where the row
        must survive but the personal-data link to the deleted user must
        not. The pseudonym itself is ``_pseudonym``'s.

        ``contexts`` rows are reached through ``locked_ids`` (#2278), locked
        in id order like every other multi-row write over that table (all of
        them are held since the hand-over, so the re-lock is free).
        ``synchronize_session="fetch"``: the hand-over loaded some of them
        into the session, which must not keep the raw sub.
        """
        statement = update(model).values({column: self._pseudonym(user_id)})
        if model is Context:
            statement = statement.where(
                Context.id.in_(
                    locked_ids(Context, lambda row: (getattr(row, column.key) == user_id,))
                )
            ).execution_options(synchronize_session="fetch")
        else:
            statement = statement.where(column == user_id)
        result = cast(CursorResult[Any], await self.db.execute(statement))
        return result.rowcount or 0

    async def _pseudonymize_audit_logs(self, target: User) -> dict[str, int]:
        """The subject's audit rows and the rows mentioning them
        (``erasure_details.pseudonymize_audit_trail``), in
        ``_delete_postgres``'s transaction (#2287, #2298).

        #2359: the addresses are ``email_holdings``'s — the current one from
        ``users.created_at`` (or its last email sync) until now, and every
        earlier one recovered from the account's email-sync rows, each
        matched on other actors' rows only while the subject held it. The
        account's earlier tenures (erased, then re-created under the same
        sub) are the re-run's (``reprocess_erasures``).

        Returns:
            The ``audit_*`` counts of the request's ``postgres`` summary.
        """
        user_id = target.user_id
        holdings = await email_holdings(
            self.db,
            [user_id, self._pseudonym(user_id)],
            now=utcnow(),
            tenures=[Tenure(start=target.created_at, email=target.email)],
        )
        kept = await count_audit_mentions_kept(self.db, holdings.addresses, exclude_user_id=user_id)
        own, mentions = await pseudonymize_audit_trail(self.db, user_id, holdings.addresses)
        return {
            "audit_logs_pseudonymized": own,
            "audit_mentions_pseudonymized": mentions,
            "audit_mentions_kept": kept,
            "audit_addresses_not_recovered": holdings.not_recovered,
        }

    async def _clear_redis(self, user_id: str) -> dict[str, int]:
        """Best-effort Redis cleanup. Failures are logged inside helpers."""
        # SessionManager uses a sync Redis client — fetch the live instance
        # the app registered at startup (#1809).
        sessions_deleted = 0
        session_manager = get_session_manager()
        if session_manager is not None:
            sessions_deleted = session_manager.delete_user_sessions(user_id)

        # #1752: pending security-notice windows (due entries, buffered
        # occurrences with IPs / user agents, pointers, claims) go too.
        from services.security_notification_service import purge_user_notification_state

        return {
            "sessions": sessions_deleted,
            "co_act": await clear_co_activations(user_id),
            "rate_limit": await clear_user_rate_limits(user_id),
            "security_notices": await purge_user_notification_state(user_id),
        }

    async def _write_audit_log(
        self, request: ErasureRequest, target: User, summary: dict[str, Any]
    ) -> None:
        """Append the canonical `account_erasure` audit row, fully pseudonymized.

        This row is born AFTER the audit-log pseudonymize step ran (inside the
        Postgres step), so we cannot rely on a later sweep to scrub it. Inline
        pseudonymization guarantees no plaintext email or user_id ever lands
        in audit_logs for this event — required for GDPR Art.5(1)(c)
        compliance. The row carries no ``ip_address`` / ``user_agent``
        (#2345): the request row clears both once the subject is erased, the
        admin-initiated case included, and this row follows it.
        """
        # #2288: the same derivation as every other pseudonym of the subject
        user_pseudonym = erasure_pseudonym(target.user_id)
        email_pseudonym = erasure_pseudonym(target.email)
        # #2345: the request row's rule (``pseudonymized_request_values``):
        # `initiated_by` is the pseudonym iff it is the subject's own sub —
        # decided by comparing with the subject, not by `is_self_service`
        # (an admin request the subject started themselves is the subject's
        # sub too). Another admin's id is audit-trail evidence and stays.
        initiated_by_value = (
            user_pseudonym if request.initiated_by == request.user_id else request.initiated_by
        )
        self.db.add(
            AuditLog(
                user_email=email_pseudonym,
                user_id=user_pseudonym,
                action=ERASURE_AUDIT_ACTION,
                resource=f"user_pseudonym:{user_pseudonym[:16]}",
                user_metadata={
                    "request_id": str(request.id),
                    "is_self_service": request.is_self_service,
                    "initiated_by": initiated_by_value,
                    "reason_code": request.reason_code,
                    "deleted_data_summary": summary,
                },
            )
        )
        await self.db.commit()

    async def _finalize(self, request: ErasureRequest, summary: dict[str, Any]) -> None:
        """Mark the erasure_requests row complete and pseudonymize its own PII.

        Done with an UPDATE rather than ORM mutation because the request
        object's session may have been touched by intermediate commits.

        #1365 review (F10): the row is retained for 5 years as *pseudonymized*
        accountability evidence (its own model docstring + the user-facing
        erasure runbook promise it is non-identifying), but nothing was ever
        pseudonymizing it. Now that the subject is actually erased, break the
        links: overwrite ``user_id`` (always the erased subject) with the
        deterministic pseudonym, and NULL the request metadata (``ip_address`` /
        ``user_agent``). ``initiated_by`` is pseudonymized ONLY on self-service
        (where it equals the erased subject); on an admin force-erase it is the
        acting admin's sub — legitimate accountability evidence, kept. Only the
        success path pseudonymizes: a failed/cancelled request means the user was
        NOT erased, so that row legitimately keeps identifying info.

        Not guarded by this run's claim (``started_at``), unlike the failure
        handlers before the Postgres commit (#2302, deciding the #2306
        review's question): ``_finalize`` only runs after this run's
        Postgres step committed, and that fact outranks any newer claim of
        the row, as in the generic handler's ``erased_in_postgres`` case. A
        newer claim found the subject either gone (the sweep closes the row
        ``interrupted``, which this completion is meant to replace, #2286)
        or still there before this run's commit — then the newer run cannot
        erase it again, and whatever it wrote (a reschedule, a failure with
        the raw ids, a cancel after a reschedule) is wrong once the subject
        is erased: ``complete`` with the pseudonymized row is the truth. The
        newer run's own writes before its Postgres commit (the reschedule and
        the failure handlers) are fenced by ``status = in_progress`` as well
        as its claim, so they cannot overwrite this completion afterwards. A
        0-row match means the row was deleted meanwhile: logged, nothing
        else (the subject is erased and the audit row is written).
        """
        values: dict[str, Any] = {
            "status": STATUS_COMPLETE,
            "completed_at": utcnow(),
            "deleted_data_summary": summary,
            # #2286: a sweep may have closed a run alive past the stale cutoff
            # as ``interrupted`` meanwhile; the run completed, so no marker
            "failure_reason": None,
            **pseudonymized_request_values(request.user_id, request.initiated_by),
        }
        request_id = request.id
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                update(ErasureRequest).where(ErasureRequest.id == request_id).values(**values)
            ),
        )
        await self.db.commit()
        if not result.rowcount:
            logger.warning("erasure_finalize_request_missing", request_id=str(request_id))

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
