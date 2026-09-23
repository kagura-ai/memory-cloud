"""Quota Service for Plan Tier Enforcement.

Issue #149: Implements quota checking and feature gating for Free/Basic/Pro plans.

Responsibilities:
- Check memory quotas before creating memories
- Check feature access (reranking, OAuth, Memory Agent)
- Check multi-workspace restrictions
- Provide quota status and warnings
"""

import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import (
    PLAN_TIERS,
    feature_denied_message,
    get_plan_tier,
    has_feature,
    lowest_tier_with_limit,
    quota_gate_details,
)
from db.redis import get_cache, incrby_counter
from models.auth import (
    Context,
    UsageStats,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.memory import Memory
from services.effective_quota_service import EffectiveQuotaService
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError, RedisError
from utils.logger import get_logger

logger = get_logger(__name__)

# Issue #1549: the day counter is keyed by UTC date, so a key outlives its day
# by at most this TTL (the date in the key keeps stale counters inert).
_MEMORIES_PER_DAY_TTL = 86400


def _memories_per_day_key(workspace_id: UUID, today: date) -> str:
    return f"quota:workspace:{workspace_id}:memories:{today.isoformat()}"


def _next_utc_midnight_iso(today: date) -> str:
    """``resets_at`` for the daily memory quota: tomorrow 00:00Z as ISO-8601."""
    return to_utc_iso(datetime.combine(today + timedelta(days=1), datetime.min.time(), UTC)) or ""


def _context_cap_upgrade_tier(max_contexts: int) -> str | None:
    """Lowest tier whose context cap beats ``max_contexts``, or ``None`` (#1644).

    Lives out here, not inline in ``check_context_creation_allowed``, so that
    function still resolves the cap ONLY through
    ``workspace.effective_max_contexts`` — naming the raw tier field inside it
    is what ``tests/api/test_context_quota_consistency.py`` forbids, because
    raw addition bypasses the zero-floor clamp.
    """
    return lowest_tier_with_limit("max_contexts_per_workspace", max_contexts)


class QuotaService:
    """Service for checking quotas and feature access based on plan tiers.

    Issue #149: Plan tier enforcement.
    """

    def __init__(self, db: AsyncSession):
        """Initialize quota service.

        Args:
            db: Database session
        """
        self.db = db

    # ========================================================================
    # Memory Quota Checks
    # ========================================================================

    async def check_memory_quota(
        self,
        workspace_id: UUID,
        raise_on_exceeded: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if workspace can create more memories.

        Args:
            workspace_id: Workspace ID
            raise_on_exceeded: If True, raise QuotaExceededError instead of returning False

        Returns:
            Tuple of (can_create, error_message)

        Raises:
            QuotaExceededError: If raise_on_exceeded=True and quota exceeded
        """
        # Issue #273 H-5: Add row-level locking to prevent race conditions
        # Get workspace with plan limits (with FOR UPDATE lock)
        workspace_result = await self.db.execute(
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .with_for_update()  # Lock workspace row during quota check
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            error = f"Workspace {workspace_id} not found"
            if raise_on_exceeded:
                raise QuotaExceededError(error)
            return False, error

        # Count memories across all workspace members (optimized single query with JOIN)
        # Issue #273 C-2: Add NULL workspace_id and deleted_at filters to prevent quota bypass
        # Note: This count is still subject to TOCTOU race conditions between check and insert.
        #       For strict enforcement, consider adding a database CHECK constraint.
        memory_count_result = await self.db.execute(
            select(func.count(Memory.id))
            .select_from(Memory)
            .join(WorkspaceMember, Memory.user_id == WorkspaceMember.user_id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                Memory.workspace_id.isnot(None),  # Exclude NULL workspace_id (orphaned memories)
                Memory.deleted_at.is_(None),  # Exclude soft-deleted memories
            )
        )
        current_count = memory_count_result.scalar() or 0

        # If no memories, workspace has no usage
        if current_count == 0:
            return True, None

        # Issue #238: Use effective quotas (base + addons)
        effective_quota_service = EffectiveQuotaService(self.db)
        effective_quotas = await effective_quota_service.get_effective_quotas(workspace_id)
        memory_limit = effective_quotas["memory_limit"]

        # Check against effective limit
        if current_count >= memory_limit:
            error = (
                f"Memory quota exceeded. "
                f"Current: {current_count}, Limit: {memory_limit} ({workspace.plan_name} plan + addons)"
            )
            logger.warning(
                "memory_quota_exceeded",
                workspace_id=str(workspace_id),
                current=current_count,
                limit=memory_limit,
                plan=workspace.plan_name,
            )

            if raise_on_exceeded:
                raise QuotaExceededError(error)
            return False, error

        return True, None

    async def check_memories_per_day(
        self,
        workspace_id: UUID,
        count: int = 1,
        *,
        raise_on_exceeded: bool = False,
    ) -> tuple[bool, str | None]:
        """Reserve ``count`` memory creations against today's daily quota (#1549).

        Redis counter ``quota:workspace:{workspace_id}:memories:{YYYY-MM-DD}``
        (UTC day, 24h TTL). Read-then-reserve is racy, so this RESERVES first:
        INCRBY ``count``; if the new total exceeds
        ``Workspace.effective_memories_per_day`` the same amount is decremented
        back and the request is refused. Concurrent overshoot is therefore
        bounded to one batch, and a refused batch is never partially charged.

        Which writes are charged (every path that creates a user-visible
        memory row):

        - ``MemoryService.remember`` (count=1) — MCP ``remember``, REST
          ``POST /memory/remember`` and the *create* half of
          ``update_memory(external_id=...)`` all funnel through it.
        - ``ResourceIndexer.process_incremental`` — connector / ``ingest_events``
          / resource ingest; charged once per batch, up front, with
          ``count = number of upsert doc_ids not yet indexed`` (a re-sync of
          known docs is free); a batch that does not fit is left untouched and
          the indexer job re-queues it for the next UTC midnight.

        Not charged (they mutate or copy rows the workspace already created):
        ``MemoryService._update_in_place`` / ``patch_memory`` /
        ``_upsert_by_external_id`` when it replaces an existing external_id,
        ``ContextService.merge_contexts`` (copies rows into the target
        context), ``api.routes.admin`` context recovery (restores rows from
        Qdrant), Sleep / consolidation / promotion (``services.sleep``,
        ``neural`` — they only re-scope, merge or soft-delete existing rows)
        and agent bootstrap (read-only).

        A reservation is not refunded if the write fails later (validation,
        DB error): a failed attempt costs one unit, like an MCP call does.
        Redis unavailable → fail-open with a warning log, exactly like
        ``RateLimitMiddleware._check_daily_quota`` (``RedisError`` caught).

        Args:
            workspace_id: Workspace ID
            count: Memories about to be created (a batch reserves all at once)
            raise_on_exceeded: If True, raise QuotaExceededError instead of
                returning False

        Returns:
            Tuple of (can_create, error_message)

        Raises:
            QuotaExceededError: If raise_on_exceeded=True and the reservation
                does not fit. ``details`` carries ``quota_type="memories_per_day"``,
                ``limit``, ``used_today``, ``requested`` and ``resets_at``.
        """
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            error = f"Workspace {workspace_id} not found"
            if raise_on_exceeded:
                raise QuotaExceededError(error)
            return False, error

        if count <= 0:
            return True, None

        limit = workspace.effective_memories_per_day
        today = utcnow().date()
        resets_at = _next_utc_midnight_iso(today)

        def _refuse(used_today: int) -> tuple[bool, str | None]:
            error = (
                f"Daily memory-creation quota exceeded. "
                f"Limit: {limit}/day ({workspace.plan_name} plan), "
                f"created today: {used_today}, requested: {count}. "
                f"Resets at {resets_at}."
            )
            logger.warning(
                "memories_per_day_exceeded",
                workspace_id=str(workspace_id),
                used_today=used_today,
                requested=count,
                limit=limit,
                plan=workspace.plan_name,
            )
            if raise_on_exceeded:
                raise QuotaExceededError(
                    error,
                    **quota_gate_details(
                        workspace.plan_name,
                        "memories_per_day",
                        current=used_today,
                        limit=limit,
                        required_plan=lowest_tier_with_limit("memories_per_day", limit),
                        resets_at=resets_at,
                    ),
                    # #1644: ``used_today`` is the legacy name for ``current``
                    # and stays beside it; ``requested`` is quota-specific.
                    used_today=used_today,
                    requested=count,
                )
            return False, error

        # Zero-floor (#569): 0 means the tier cannot create memories at all.
        # Nothing to reserve, so Redis is not touched.
        if limit == 0:
            return _refuse(0)

        key = _memories_per_day_key(workspace_id, today)
        try:
            new_total = await incrby_counter(key, count, ttl=_MEMORIES_PER_DAY_TTL)
        except RedisError as e:
            # Fail-open: never block a write because the counter is down.
            logger.warning(
                "memories_per_day_redis_failed",
                workspace_id=str(workspace_id),
                error=str(e),
            )
            return True, None

        if new_total > limit:
            # Release the reservation so the refused attempt does not consume
            # budget. Best-effort: if this fails the phantom reservation
            # expires with the day key.
            try:
                await incrby_counter(key, -count)
            except RedisError as e:
                logger.warning(
                    "memories_per_day_release_failed",
                    workspace_id=str(workspace_id),
                    count=count,
                    error=str(e),
                )
            return _refuse(new_total - count)

        return True, None

    async def count_memories_created_today(
        self, workspace_id: UUID, *, today: date | None = None
    ) -> int:
        """Memories created in this workspace today (UTC), per the #1549 counter.

        Read-only GET on the day key; 0 when missing or Redis is unavailable.
        ``today`` lets a caller that also derives ``resets_at`` read the clock
        once, so a midnight crossing cannot pair one day's count with the
        next day's reset.
        """
        if today is None:
            today = utcnow().date()
        cached = await get_cache(_memories_per_day_key(workspace_id, today))
        try:
            return int(cached) if cached else 0
        except ValueError:
            return 0

    # ========================================================================
    # Feature Access Checks
    # ========================================================================

    async def check_feature_access(
        self,
        workspace_id: UUID,
        feature: str,
        raise_on_denied: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if workspace's plan includes a feature.

        Args:
            workspace_id: Workspace ID
            feature: Feature name (e.g., 'reranking', 'oauth', 'team_invitations')
            raise_on_denied: If True, raise FeatureNotAvailableError

        Returns:
            Tuple of (has_access, error_message)

        Raises:
            FeatureNotAvailableError: If raise_on_denied=True and feature not available
        """
        # Get workspace
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            error = f"Workspace {workspace_id} not found"
            if raise_on_denied:
                raise FeatureNotAvailableError(error)
            return False, error

        # Check if plan includes feature
        if not has_feature(workspace.plan_name, feature):
            # Get required plan tier
            from config.plan_tiers import get_required_plan_for_feature

            try:
                required_plan = get_required_plan_for_feature(feature)
            except ValueError:
                required_plan = "unknown"

            # Shared with the create gates that already hold the workspace
            # row (#1551) so every refusal derives the tier the same way.
            error = feature_denied_message(workspace.plan_name, feature)
            logger.info(
                "feature_access_denied",
                workspace_id=str(workspace_id),
                feature=feature,
                plan=workspace.plan_name,
                required_plan=required_plan,
            )

            if raise_on_denied:
                raise FeatureNotAvailableError.for_feature(workspace.plan_name, feature)
            return False, error

        return True, None

    # ========================================================================
    # Multi-workspace Restrictions
    # ========================================================================

    async def check_workspace_creation_allowed(
        self,
        user_id: str,
        raise_on_denied: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if user can create another workspace.

        Issue #276 (updated by Issue #661, refined by #674/#675, #1550):
        the owned-workspace cap is ``1 (base) + users.workspace_slot_bonus
        + owned_workspace_grant`` of the highest tier the user owns
        (free 0 / basic 0 / pro 2 / promax 19). Block-new-only: a user
        above the cap (e.g. after downgrading that workspace) keeps every
        workspace — only creating another is refused here. Joined
        workspaces (via invite) do not count toward this limit — they
        consume the inviting workspace's seat quota, which the inviter
        pays for.

        Issue #677 (sub-C): a per-user ``pg_advisory_xact_lock`` is
        acquired before the count/cap read to close the TOCTOU race
        where two concurrent create paths could each observe
        ``count < cap`` and both insert. The lock is xact-scoped, so
        the caller must wrap the cap check and the workspace insert in
        the same transaction for the serialization to extend across
        the insert.

        Rollout (Issue #661, refined by #677): when
        ``settings.enforce_workspace_cap`` is False (default), the
        method logs over-cap creates but still returns OK so affected
        accounts surface via telemetry. Lock-acquire failures
        (``lock_timeout`` or unexpected DB errors) follow a hybrid fail
        policy: deny when ``enforce=True`` (cap is the safety
        invariant), allow + log when ``enforce=False`` (log-only mode
        must not generate false denials).

        Args:
            user_id: User ID
            raise_on_denied: If True, raise QuotaExceededError

        Returns:
            Tuple of (can_create, error_message)

        Raises:
            QuotaExceededError: If raise_on_denied=True, the limit is
                reached, AND ``settings.enforce_workspace_cap`` is True.
        """
        from config.settings import get_settings
        from utils.plan_resolver import (
            cap_on_tier,
            get_user_workspace_cap_summary,
            next_tier_with_more_workspaces,
        )

        settings = get_settings()

        # Issue #677 (sub-C): acquire a per-user advisory lock so the
        # subsequent count/cap read and the caller's workspace insert
        # serialize per user_id. On lock_timeout / DB error the
        # transaction is in error state and MUST be rolled back before
        # any further statement — Postgres rejects everything until then.
        # Hybrid fail policy: deny when enforced (cap is the safety
        # invariant), allow when not enforced (log-only must not
        # produce false denials).
        lock_wait_ms: float | None = None
        try:
            lock_wait_ms = await self._acquire_workspace_create_lock(user_id)
        except DBAPIError as exc:
            # Catch the broader SQLAlchemy DBAPI wrapper so we cover every
            # driver mapping for SQLSTATE 55P03 — asyncpg has historically
            # mapped lock-cancellation to several exception subclasses, so
            # narrowing to OperationalError would let real lock_timeouts
            # bypass this branch (PR #686 Copilot review). asyncpg surfaces
            # the SQLSTATE as ``sqlstate``; psycopg2 as ``pgcode``.
            sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            reason = "lock_timeout" if sqlstate == "55P03" else "lock_error"
            logger.warning(
                "workspace_create_lock_failed",
                user_id=user_id,
                reason=reason,
                sqlstate=sqlstate,
                enforced=settings.enforce_workspace_cap,
            )
            # The session is poisoned until rollback — issue it before
            # we either deny or fall back to allow.
            await self.db.rollback()
            if settings.enforce_workspace_cap:
                error = "Workspace creation temporarily unavailable. Please retry in a moment."
                if raise_on_denied:
                    raise QuotaExceededError(error) from exc
                return False, error
            # enforce=False: log-only mode must not generate false
            # denials on infrastructure errors — allow the create.
            return True, None

        # Issue #675 (epic #674 sub-A) / #1550: cap = 1 (base) +
        # users.workspace_slot_bonus + owned_workspace_grant of the highest
        # tier the user owns. The plan_resolver helper resolves everything
        # in a single SELECT (JOIN of users + workspaces) so the gate and
        # the dashboard read consistent state.
        summary = await get_user_workspace_cap_summary(self.db, user_id)
        workspace_count, cap = summary.owned_count, summary.cap

        if workspace_count >= cap:
            # #1550 upsell-ready refusal: name the tier the cap derives from
            # and the next tier that grants more slots (None on the top tier,
            # in which case the upgrade sentence is dropped).
            next_tier = next_tier_with_more_workspaces(summary.tier)
            upsell = ""
            if next_tier is not None:
                next_display = get_plan_tier(next_tier).display_name
                next_cap = cap_on_tier(summary, next_tier)
                upsell = f"Upgrade to {next_display} to own up to {next_cap}. "
            error = (
                f"Workspace limit reached. "
                f"You currently own {workspace_count} workspace(s) "
                f"(cap: {cap} on the {get_plan_tier(summary.tier).display_name} plan). "
                f"{upsell}You can still join other workspaces as a member via invite."
            )
            logger.warning(
                "workspace_creation_denied",
                user_id=user_id,
                current_owned_workspaces=workspace_count,
                max_owned_workspaces=cap,
                tier=summary.tier,
                tier_grant=summary.tier_grant,
                enforced=settings.enforce_workspace_cap,
                lock_wait_ms=lock_wait_ms,
                reason="over_cap",
            )

            # Issue #661 rollout gate: when the flag is off, log but allow.
            if not settings.enforce_workspace_cap:
                return True, None

            if raise_on_denied:
                # Issue #680: carry structured fields in ``details`` so clients
                # (e.g. WorkspaceCreateForm) can localize the message instead of
                # surfacing the English string verbatim. ``quota_type`` is the
                # discriminator the frontend keys off (``error`` stays the shared
                # ``QUOTA-001``); ``owned_count`` / ``cap`` feed the i18n
                # placeholders. Future quota types follow the same convention.
                # #1550: ``tier`` / ``next_tier`` are plan KEYS (not display
                # names) so the client can localize the upsell itself.
                raise QuotaExceededError(
                    error,
                    **quota_gate_details(
                        summary.tier,
                        "workspace_limit_reached",
                        current=workspace_count,
                        limit=cap,
                        # The next tier that grants more slots is already
                        # resolved for the message; it IS the upgrade path.
                        required_plan=next_tier,
                    ),
                    # #1644: ``owned_count`` / ``cap`` are the legacy names of
                    # ``current`` / ``limit`` and are kept verbatim — an older
                    # client (WorkspaceCreateForm) reads them.
                    owned_count=workspace_count,
                    cap=cap,
                    tier=summary.tier,
                    next_tier=next_tier,
                )
            return False, error

        # Info-level success log so the 7-day observation window can
        # measure lock_wait_ms p99 across normal (under-cap) creates,
        # not just denials — without this, contention is invisible
        # until users hit the cap (PR #686 loop 4 review).
        logger.info(
            "workspace_create_gate_passed",
            user_id=user_id,
            current_owned_workspaces=workspace_count,
            max_owned_workspaces=cap,
            tier=summary.tier,
            tier_grant=summary.tier_grant,
            lock_wait_ms=lock_wait_ms,
            enforced=settings.enforce_workspace_cap,
        )
        return True, None

    async def _acquire_workspace_create_lock(self, user_id: str) -> float:
        """Acquire a per-user advisory lock for workspace-creation cap gating.

        Issue #677 (sub-C): serializes concurrent create paths for the
        same user so the cap check and the caller's insert behave as
        one critical section. The lock is xact-scoped — Postgres releases
        it on commit/rollback, so the calling transaction must hold both
        the cap check and the insert for the lock to fully serialize the
        read-then-write.

        ``SET LOCAL lock_timeout = '5s'`` keeps a pathologically long
        peer transaction from stalling our worker indefinitely. On
        timeout Postgres raises SQLSTATE 55P03 (``lock_not_available``);
        the caller maps that to fail-closed vs fail-open via
        ``settings.enforce_workspace_cap``.

        ``hashtextextended(:key, 0)`` returns a 64-bit hash matching
        the bigint signature of single-key ``pg_advisory_xact_lock``.
        At 64 bits the birthday-paradox collision probability is
        negligible (~2^32 users for 50%) — vs ``hashtext`` which is
        32-bit and would collide at ~65k users, potentially causing
        unrelated users to block each other under load (PR #686
        loop 4 review).

        Coverage note: this helper only serializes callers of
        ``check_workspace_creation_allowed``. The auto-create paths
        ``WorkspaceService.ensure_personal_workspace`` and
        ``ContextService._ensure_personal_workspace`` insert personal
        workspaces directly without going through this gate. Closing
        the cap on those paths is tracked separately (out of scope
        for #677, which is the user-initiated ``POST /workspaces``
        gate).

        Args:
            user_id: User ID (OAuth ``sub`` claim).

        Returns:
            Elapsed wait time in milliseconds (float — sub-millisecond
            precision matters when distinguishing "fast path, no
            contention" from "lock granted immediately after queue
            drain"). Measurement scope is the advisory-lock acquire
            statement only — the preceding ``SET LOCAL`` round-trip is
            excluded, so the value reflects time spent waiting for the
            lock plus the single SELECT round-trip (typically <2 ms
            without contention).
        """
        lock_key = f"workspace_create:{user_id}"

        # SET LOCAL applies to the rest of the current transaction, not
        # just to the immediately-following statement. The caller's
        # transaction continues past this helper into the workspace
        # INSERT (and any other statements WorkspaceService.create_workspace
        # issues), which must NOT inherit a 5s lock_timeout — without the
        # reset below they would unexpectedly time out on any lock wait
        # (PR #686 Copilot review). The acquire is bracketed by SET to
        # 5s on entry and reset to '0' (no timeout, session default) on
        # exit so the remainder of the caller's transaction is unaffected.
        await self.db.execute(text("SET LOCAL lock_timeout = '5s'"))

        start = time.monotonic()
        acquired = False
        try:
            await self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(
                    key=lock_key
                )
            )
            acquired = True
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000

        # Reset is OUTSIDE the try/finally: we only run it on success, and
        # we let any reset error bubble up so the caller sees the poisoned
        # session and applies the lock-error policy (PR #686 loop 3 review).
        # When ``acquired`` is False, the tx is in error state from the
        # acquire failure — the caller's except branch will rollback and
        # clear the lock_timeout setting along with it.
        if acquired:
            await self.db.execute(text("SET LOCAL lock_timeout = '0'"))
        return elapsed_ms

    async def check_context_creation_allowed(
        self,
        workspace_id: UUID,
        raise_on_denied: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if workspace can create another context.

        Free plan: Max 1 context/workspace
        Basic/Pro: Unlimited contexts

        Args:
            workspace_id: Workspace ID
            raise_on_denied: If True, raise QuotaExceededError

        Returns:
            Tuple of (can_create, error_message)

        Raises:
            QuotaExceededError: If raise_on_denied=True and limit reached
        """
        # Get workspace
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            error = f"Workspace {workspace_id} not found"
            if raise_on_denied:
                raise QuotaExceededError(error)
            return False, error

        # Get effective limit (plan base + addon bonus).
        #
        # Go through the model property rather than adding the two numbers here
        # (#1487): `effective_max_contexts` applies `_zero_floor`, so a tier
        # whose base is 0 cannot be lifted by an addon. This was the only
        # context-cap site doing raw addition, which meant the number enforced on
        # create could disagree with the number every other site — and now the
        # API response — reports.
        plan = get_plan_tier(workspace.plan_name)
        max_contexts = workspace.effective_max_contexts

        # Count current contexts
        context_count_result = await self.db.execute(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
        context_count = context_count_result.scalar() or 0

        # Check against limit
        if context_count >= max_contexts:
            # #1644: the upgrade tier comes from the registry, not a literal.
            # "Upgrade to Basic or Pro plan" sat one line under an
            # interpolated ``plan.display_name``, so under a display-name
            # override the same sentence named the same tier two ways. The
            # sentence is DROPPED entirely when no tier raises the cap, so the
            # message never promises an upgrade that does not exist.
            upgrade = _context_cap_upgrade_tier(max_contexts)
            error = (
                f"Context limit reached. "
                f"Your {plan.display_name} plan allows {max_contexts} context(s) per workspace."
            ) + (
                f" Upgrade to {PLAN_TIERS[upgrade].display_name} plan for more contexts."
                if upgrade
                else ""
            )
            logger.warning(
                "context_creation_denied",
                workspace_id=str(workspace_id),
                current_contexts=context_count,
                max_contexts=max_contexts,
                plan=workspace.plan_name,
            )

            if raise_on_denied:
                # #1644 S3: this cap used to be the one quota refusal with no
                # ``quota_type`` and no counts at all.
                raise QuotaExceededError(
                    error,
                    **quota_gate_details(
                        workspace.plan_name,
                        "contexts",
                        current=context_count,
                        limit=max_contexts,
                        required_plan=upgrade,
                    ),
                )
            return False, error

        return True, None

    # ========================================================================
    # Member Quota Checks (Issue #229)
    # ========================================================================

    async def check_member_quota(
        self,
        workspace_id: UUID,
        raise_on_exceeded: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if workspace can invite more members.

        Counts both current members and pending (non-expired) invitations
        to prevent over-inviting.

        Args:
            workspace_id: Workspace ID
            raise_on_exceeded: If True, raise QuotaExceededError

        Returns:
            Tuple of (can_invite, error_message)

        Raises:
            QuotaExceededError: If raise_on_exceeded=True and quota exceeded

        Issue #229: Implement team member limit (10 members max for Pro plan)
        """
        # Get workspace
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            error = f"Workspace {workspace_id} not found"
            if raise_on_exceeded:
                raise QuotaExceededError(error)
            return False, error

        # Count current members
        member_count_result = await self.db.execute(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
        member_count = member_count_result.scalar() or 0

        # Count pending invitations (not accepted, not expired)
        pending_count_result = await self.db.execute(
            select(func.count(WorkspaceInvitation.id)).where(
                WorkspaceInvitation.workspace_id == workspace_id,
                WorkspaceInvitation.accepted_at.is_(None),
                or_(
                    WorkspaceInvitation.expires_at.is_(None),
                    WorkspaceInvitation.expires_at > utcnow(),
                ),
            )
        )
        pending_count = pending_count_result.scalar() or 0

        total_used = member_count + pending_count

        # Check limit using EffectiveQuotaService to avoid drift
        from services.effective_quota_service import EffectiveQuotaService

        effective = await EffectiveQuotaService(self.db).get_effective_quotas(workspace_id)
        max_members = effective["max_members"]
        if total_used >= max_members:
            error = (
                f"Member limit reached ({max_members} seats). "
                f"Current members: {member_count}, Pending invitations: {pending_count}. "
                f"Upgrade your plan or add member slots to invite more."
            )
            logger.warning(
                "member_quota_exceeded",
                workspace_id=str(workspace_id),
                member_count=member_count,
                pending_count=pending_count,
                total_used=total_used,
                limit=max_members,
                plan=workspace.plan_name,
            )

            if raise_on_exceeded:
                raise QuotaExceededError(
                    error,
                    **quota_gate_details(
                        workspace.plan_name,
                        "members",
                        current=total_used,
                        limit=max_members,
                        required_plan=lowest_tier_with_limit(
                            "max_members_per_workspace", max_members
                        ),
                        feature="team_invitations",
                    ),
                )
            return False, error

        return True, None

    # ========================================================================
    # MCP Rate Limit (Issue #149)
    # ========================================================================

    async def count_mcp_calls_today(self, workspace_id: UUID) -> int:
        """Count today's MCP tool calls for a workspace.

        Lightweight helper — only runs the COUNT query without fetching workspace.
        Used by get_usage to avoid redundant workspace lookup.

        Args:
            workspace_id: Workspace ID

        Returns:
            Number of MCP calls today
        """
        today = utcnow().date()
        count_result = await self.db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.workspace_id == workspace_id,
                UsageStats.date == today,
                UsageStats.method == "MCP",
            )
        )
        return count_result.scalar() or 0

    async def check_mcp_rate_limit(
        self,
        workspace_id: UUID,
    ) -> tuple[bool, int, int]:
        """Check if workspace has remaining MCP calls for today.

        Counts today's MCP tool calls from usage_stats and compares
        against effective_mcp_calls_per_day quota.

        Uses existing idx_usage_stats_workspace_date index.

        Args:
            workspace_id: Workspace ID

        Returns:
            Tuple of (allowed, used_today, daily_limit).
            allowed=False when used_today >= daily_limit.

        Raises:
            ValueError: If workspace not found
        """
        # Fetch workspace first to short-circuit on missing workspace before COUNT
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")

        today = utcnow().date()

        count_result = await self.db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.workspace_id == workspace_id,
                UsageStats.date == today,
                UsageStats.method == "MCP",
            )
        )
        used_today = count_result.scalar() or 0

        daily_limit = workspace.effective_mcp_calls_per_day

        if used_today >= daily_limit:
            logger.warning(
                "mcp_rate_limit_exceeded",
                workspace_id=str(workspace_id),
                used_today=used_today,
                daily_limit=daily_limit,
                plan=workspace.plan_name,
            )
            return False, used_today, daily_limit

        return True, used_today, daily_limit

    # ========================================================================
    # Quota Status
    # ========================================================================

    async def get_quota_status(self, workspace_id: UUID) -> dict[str, Any]:
        """Get comprehensive quota status for workspace.

        Returns current usage, limits, and warning flags.

        Args:
            workspace_id: Workspace ID

        Returns:
            Dict with quota status:
                - memory: {current, limit, percentage, warning, exceeded}
                - memories_today: {current, limit, percentage, warning, exceeded,
                  resets_at} (#1549 daily memory-creation quota)
                - features: {reranking, oauth} (bool)
        """
        # Get workspace
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            return {}

        # Get plan tier
        plan = get_plan_tier(workspace.plan_name)

        # Get all member user_ids
        members_result = await self.db.execute(
            select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == workspace_id)
        )
        member_ids = [row[0] for row in members_result.all()]

        # Calculate memory usage
        # Issue #273 C-2: Add NULL workspace_id and deleted_at filters to prevent quota bypass
        if member_ids:
            memory_count_result = await self.db.execute(
                select(func.count(Memory.id)).where(
                    Memory.user_id.in_(member_ids),
                    Memory.workspace_id.isnot(
                        None
                    ),  # Exclude NULL workspace_id (orphaned memories)
                    Memory.deleted_at.is_(None),  # Exclude soft-deleted memories
                )
            )
            memory_count = memory_count_result.scalar() or 0
        else:
            memory_count = 0

        # Calculate percentages
        effective_limit = workspace.effective_memory_limit
        memory_percentage = (memory_count / effective_limit * 100) if effective_limit > 0 else 0

        # Issue #1549: today's memory creations vs the daily quota (Redis read).
        # One clock read for both the counter key and resets_at.
        today = utcnow().date()
        created_today = await self.count_memories_created_today(workspace_id, today=today)
        daily_limit = workspace.effective_memories_per_day
        daily_percentage = (created_today / daily_limit * 100) if daily_limit > 0 else 0

        return {
            "memory": {
                "current": memory_count,
                "limit": effective_limit,
                "percentage": round(memory_percentage, 2),
                "warning": memory_percentage >= 80,
                "exceeded": memory_percentage >= 100,
            },
            "memories_today": {
                "current": created_today,
                "limit": daily_limit,
                "percentage": round(daily_percentage, 2),
                "warning": daily_percentage >= 80,
                "exceeded": daily_percentage >= 100,
                "resets_at": _next_utc_midnight_iso(today),
            },
            "features": {
                "reranking": "reranking" in plan.features,
                "oauth": "oauth" in plan.features,
            },
            "plan": {
                "name": workspace.plan_name,
                "display_name": plan.display_name,
            },
        }
