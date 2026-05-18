"""Per-workspace embedding spend cap enforcement (#709).

Issue #709: prereq for #708 Option A (shared-context reads charge the
shared-source workspace owner's BYOK key). This service tracks daily and
monthly USD spend per workspace in Redis (independent of ``llm_call_log``
so cap accounting works even when the writer path is degraded) and gates
embedding calls on workspace-effective caps from ``Workspace`` / ``PlanTier``.

Architecture (HYBRID plan, see issue thread):

- **Pre-call gate** (``check_cap_or_raise``): read the current spend from
  Redis, raise ``EmbeddingSpendCapExceeded`` if it has reached the
  effective cap. Called from ``EmbeddingService.embed_with_usage`` before
  the external API call so over-cap requests never hit OpenAI/Voyage/etc.
- **Post-call record** (``record_spend``): atomic ``INCRBY`` of the
  micro-USD counter (avoids float drift across increments). Fires the
  80% / 100% alert email with a Redis SETNX dedup so threshold crossings
  email at most once per period.
- **Pass-through**: if neither daily nor monthly cap is set on the
  workspace OR its tier, both methods are no-ops. Free / Basic / Pro
  tier defaults live in ``config/plan_tiers.py``.

Redis layout:
    embed_spend:{workspace_id}:daily:{YYYY-MM-DD}      (TTL 25h)
    embed_spend:{workspace_id}:monthly:{YYYY-MM}       (TTL 32 days)
    embed_spend_alert:{ws}:{period}:{date}:{pct}       (TTL = counter TTL)

The counter holds an **integer count of micro-USD** (``Decimal × 1_000_000``).
Storing the running total as an integer means ``INCRBY`` arithmetic is exact
across thousands of small calls — float would drift after a few hundred
sub-cent embeddings.

Fail-safe behavior:
    Redis outages do NOT block embedding calls. ``check_cap_or_raise``
    treats a ``RedisError`` as "spend is 0" (pass-through); ``record_spend``
    swallows the increment failure and logs. The advisory rate-limit
    pattern in ``resource_quota_service.py`` does the same — cap is
    "best-effort enforcement", not "hard guarantee".

Alert path:
    Resend email via ``EmailService.send_embedding_spend_alert``. Awaited
    inline rather than ``asyncio.create_task``-d — every other email caller
    in this codebase awaits, and a fire-and-forget task here would (1) drop
    the only strong reference so CPython can GC it before completion, and
    (2) outlive the request-scoped ``self.db`` used inside the alert path
    for the owner-email lookup. The Resend SDK call already runs in
    ``asyncio.to_thread`` and ``_send`` returns ``False`` on failure, so
    the await cost is bounded to the SDK thread hop.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import select

from db.redis import get_cache, get_redis_client, incrby_counter
from models.auth import User, Workspace
from services.email_service import EmailService, get_email_service
from utils.datetime import utcnow
from utils.exceptions import EmbeddingSpendCapExceeded, RedisError
from utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

# Daily counter survives a 1h clock skew past UTC midnight (the alert dedup
# key shares this TTL, so a workspace that crosses 80% at 23:55 UTC still
# sees the dedup expire when the counter rolls over to the next day).
_DAILY_TTL_SECONDS = 25 * 3600
_MONTHLY_TTL_SECONDS = 32 * 24 * 3600

_MICRO_USD = Decimal(1_000_000)

# Pinned to a tuple for runtime membership checks; the matching ``Literal``
# alias gives static-check coverage on every caller. Mirrors the
# ``LLM_CALL_LOG_PAID_BY_VALUES`` / ``Literal`` pattern in ``models/llm_call_log.py``.
EMBEDDING_SPEND_CAP_PERIODS: tuple[str, ...] = ("daily", "monthly")
EmbeddingSpendCapPeriod = Literal["daily", "monthly"]

_ALERT_THRESHOLDS_PCT: tuple[Literal[80, 100], ...] = (80, 100)


class EmbeddingSpendCapService:
    """Pre-call gate and post-call accumulator for BYOK embedding spend.

    Construct one per request (DB session is request-scoped). The service
    itself holds no mutable state — counters live in Redis, caps live on
    the ``Workspace`` row.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        email: EmailService | None = None,
    ) -> None:
        self.db = db
        # ``email`` defaults to ``None`` and resolves via ``get_email_service()``
        # at send time so tests that swap the singleton via
        # ``reset_email_service_for_testing`` after construction still
        # see the patched instance.
        self._email_override = email

    @property
    def email(self) -> EmailService:
        return self._email_override or get_email_service()

    async def check_cap_or_raise(self, workspace: Workspace) -> None:
        """Pre-call gate. Raise ``EmbeddingSpendCapExceeded`` if at/over cap.

        No-op when neither daily nor monthly cap is configured (uncapped
        workspace). Redis read failures are treated as "no spend yet" and
        let the call proceed — the post-call ``record_spend`` will still
        attempt to increment the counter, so a transient Redis outage
        cannot let a single over-cap request through more than once.
        """
        daily_cap = workspace.effective_embedding_daily_cap_usd
        monthly_cap = workspace.effective_embedding_monthly_cap_usd

        if daily_cap is not None:
            daily_spent = await self._read_spend("daily", workspace.id)
            if daily_spent >= daily_cap:
                raise EmbeddingSpendCapExceeded(
                    f"Daily embedding spend cap reached (${daily_cap:.4f}/day)",
                    period="daily",
                    cap_usd=float(daily_cap),
                    current_usd=float(daily_spent),
                    workspace_id=str(workspace.id),
                )

        if monthly_cap is not None:
            monthly_spent = await self._read_spend("monthly", workspace.id)
            if monthly_spent >= monthly_cap:
                raise EmbeddingSpendCapExceeded(
                    f"Monthly embedding spend cap reached (${monthly_cap:.4f}/month)",
                    period="monthly",
                    cap_usd=float(monthly_cap),
                    current_usd=float(monthly_spent),
                    workspace_id=str(workspace.id),
                )

    async def record_spend(
        self,
        workspace: Workspace,
        cost_usd: Decimal,
    ) -> None:
        """Post-call. Increment daily + monthly counters. Maybe alert.

        ``cost_usd`` is the BYOK-attributable cost of one embedding call
        (caller's responsibility to determine BYOK vs platform). Tiny
        sub-cent costs are stored as integer micro-USD to avoid float
        drift across many INCRs. Counters are advanced regardless of
        whether a cap is configured — admin "current spend" panels read
        them for visibility on uncapped workspaces.

        Alerts only fire when a cap exists. Each threshold (80%, 100%)
        emits at most one email per period via Redis SETNX dedup.
        """
        if cost_usd <= 0:
            return

        delta_micro = int(cost_usd * _MICRO_USD)
        if delta_micro <= 0:
            # Rounded to 0 — skip the Redis round trip.
            return

        daily_cap = workspace.effective_embedding_daily_cap_usd
        monthly_cap = workspace.effective_embedding_monthly_cap_usd

        periods: tuple[tuple[EmbeddingSpendCapPeriod, int, Decimal | None], ...] = (
            ("daily", _DAILY_TTL_SECONDS, daily_cap),
            ("monthly", _MONTHLY_TTL_SECONDS, monthly_cap),
        )
        for period, ttl, cap in periods:
            try:
                new_count_micro = await incrby_counter(
                    self._counter_key(period, workspace.id),
                    delta_micro,
                    ttl=ttl,
                )
            except RedisError:
                # Fail-open: cap is advisory — never block on Redis outage.
                logger.warning(
                    "embed_spend_counter_incr_failed",
                    workspace_id=str(workspace.id),
                    period=period,
                )
                continue

            if cap is None or cap <= 0:
                continue

            new_count = Decimal(new_count_micro) / _MICRO_USD
            await self._maybe_alert(period, workspace, new_count, cap)

    async def load_workspace(self, workspace_id: UUID | str) -> Workspace | None:
        """Fetch the ``Workspace`` row for cap resolution. Returns ``None`` if missing.

        Exposed so callers (``EmbeddingService.embed_with_usage`` / ``embed_batch``)
        can resolve the workspace ONCE per call and reuse it for both
        ``check_cap_or_raise`` and ``record_spend_from_tokens`` — avoiding
        a duplicate SELECT per embedding hot-path invocation.
        """
        ws_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
        result = await self.db.execute(select(Workspace).where(Workspace.id == ws_uuid))
        return result.scalar_one_or_none()

    async def record_spend_from_tokens(
        self,
        workspace: Workspace,
        *,
        provider: str,
        model: str,
        tokens: int,
        occurred_at: datetime | None = None,
    ) -> None:
        """Compute USD cost from ``tokens`` via ``LLMPricingService`` and record.

        No-op when ``tokens <= 0`` (cache hit), when pricing lookup misses
        (no row in ``llm_pricing`` for the (provider, model) at the given
        ``started_at``), or when computed cost rounds to zero in micro-USD.

        ``occurred_at`` lets sleep paths replay historical embeddings against
        the correct pricing snapshot; default is ``utcnow()`` for the live
        recall path.
        """
        if tokens <= 0:
            return
        # Defer the import to avoid a service-layer import cycle at module
        # load time (``llm_pricing_service`` imports from ``models`` which
        # imports from ``db``; cap service is imported from ``embedding_service``
        # which is at the same layer).
        from services.llm_pricing_service import LLMPricingService

        pricing = LLMPricingService(self.db)
        try:
            cost = await pricing.compute_cost_usd(
                provider=provider,
                model=model,
                unit_type="embedding_tokens",
                started_at=occurred_at or utcnow(),
                units=tokens,
            )
        except ValueError:
            # Invalid provider / model — pricing service raises rather than
            # silently miss. Treat the same way as a pricing miss for cap
            # purposes (don't block the call, log for ops).
            logger.warning(
                "embed_spend_pricing_invalid_input",
                workspace_id=str(workspace.id),
                provider=provider,
                model=model,
            )
            return
        if cost is None or cost <= 0:
            return
        await self.record_spend(workspace, Decimal(str(cost)))

    async def get_current_spend(self, workspace_id: UUID | str) -> tuple[Decimal, Decimal]:
        """Return ``(daily_spent_usd, monthly_spent_usd)`` for admin display.

        Safe to call on any workspace regardless of cap configuration.
        Redis read failures return ``(0, 0)`` — admin dashboards should
        treat 0 as "unknown" when paired with an out-of-band Redis alert.
        """
        ws_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
        daily = await self._read_spend("daily", ws_uuid)
        monthly = await self._read_spend("monthly", ws_uuid)
        return daily, monthly

    # -------- internals --------

    async def _read_spend(self, period: EmbeddingSpendCapPeriod, workspace_id: UUID) -> Decimal:
        try:
            cached = await get_cache(self._counter_key(period, workspace_id))
        except Exception:  # noqa: BLE001 — Redis outage is fail-open here
            return Decimal(0)
        if not cached:
            return Decimal(0)
        try:
            return Decimal(int(cached)) / _MICRO_USD
        except (ValueError, ArithmeticError):
            logger.warning(
                "embed_spend_counter_parse_failed",
                workspace_id=str(workspace_id),
                period=period,
            )
            return Decimal(0)

    async def _maybe_alert(
        self,
        period: EmbeddingSpendCapPeriod,
        workspace: Workspace,
        current_usd: Decimal,
        cap_usd: Decimal,
    ) -> None:
        if cap_usd <= 0:
            return
        ratio = current_usd / cap_usd
        # Highest crossed threshold wins so a single call that jumps past
        # both 80% and 100% reports 100% (not a stale 80% notification).
        crossed: Literal[80, 100] | None = None
        for pct in sorted(_ALERT_THRESHOLDS_PCT, reverse=True):
            if ratio * 100 >= pct:
                crossed = pct
                break
        if crossed is None:
            return

        dedup_key = self._alert_dedup_key(period, workspace.id, crossed)
        client = get_redis_client()
        try:
            ttl = _DAILY_TTL_SECONDS if period == "daily" else _MONTHLY_TTL_SECONDS
            acquired = await client.set(dedup_key, "1", nx=True, ex=ttl)
        except Exception as exc:  # noqa: BLE001 — Redis dedup is fail-open
            logger.warning(
                "embed_spend_alert_dedup_failed",
                workspace_id=str(workspace.id),
                period=period,
                pct=crossed,
                error_type=type(exc).__name__,
            )
            return
        if not acquired:
            return

        # Await the alert send rather than ``asyncio.create_task`` — every
        # other ``EmailService`` caller in this codebase awaits, and a
        # fire-and-forget task here would (1) drop the only strong reference
        # so CPython can GC it before completion, and (2) outlive the
        # request-scoped ``self.db`` used inside ``_send_alert_email`` for
        # the owner-email lookup. Resend's ``_send`` already returns False
        # on failure (no raise), and the SDK call runs in ``asyncio.to_thread``,
        # so the latency cost is bounded to the thread hop.
        await self._send_alert_email(
            workspace=workspace,
            period=period,
            current_usd=current_usd,
            cap_usd=cap_usd,
            threshold_pct=crossed,
        )

    async def _send_alert_email(
        self,
        *,
        workspace: Workspace,
        period: EmbeddingSpendCapPeriod,
        current_usd: Decimal,
        cap_usd: Decimal,
        threshold_pct: Literal[80, 100],
    ) -> None:
        try:
            result = await self.db.execute(
                select(User.email).where(User.user_id == workspace.owner_user_id)
            )
            owner_email = result.scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 — never raise into create_task
            logger.warning(
                "embed_spend_alert_owner_lookup_failed",
                workspace_id=str(workspace.id),
                error_type=type(exc).__name__,
            )
            return

        if not owner_email:
            logger.warning(
                "embed_spend_alert_no_owner_email",
                workspace_id=str(workspace.id),
                owner_user_id=workspace.owner_user_id,
            )
            return

        try:
            await self.email.send_embedding_spend_alert(
                to_email=owner_email,
                workspace_id=str(workspace.id),
                workspace_name=workspace.name,
                period=period,
                current_usd=float(current_usd),
                cap_usd=float(cap_usd),
                threshold_pct=threshold_pct,
            )
        except Exception as exc:  # noqa: BLE001 — never raise into create_task
            logger.warning(
                "embed_spend_alert_email_failed",
                workspace_id=str(workspace.id),
                period=period,
                pct=threshold_pct,
                error_type=type(exc).__name__,
            )

    @staticmethod
    def _period_bucket(period: EmbeddingSpendCapPeriod) -> str:
        """Return the date-or-month suffix for the given period."""
        return utcnow().strftime("%Y-%m-%d" if period == "daily" else "%Y-%m")

    @staticmethod
    def _counter_key(period: EmbeddingSpendCapPeriod, workspace_id: UUID) -> str:
        return (
            f"embed_spend:{workspace_id}:{period}:{EmbeddingSpendCapService._period_bucket(period)}"
        )

    @staticmethod
    def _alert_dedup_key(
        period: EmbeddingSpendCapPeriod,
        workspace_id: UUID,
        threshold_pct: Literal[80, 100],
    ) -> str:
        return (
            f"embed_spend_alert:{workspace_id}:{period}:"
            f"{EmbeddingSpendCapService._period_bucket(period)}:{threshold_pct}"
        )
