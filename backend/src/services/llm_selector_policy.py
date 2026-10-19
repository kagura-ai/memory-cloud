"""Whether a context's recall LLM selector is active (#2144).

A context stores ``llm_selector_enabled`` (its owner's choice). Where the
deployment gates it by plan (``LLM_SELECTOR_REQUIRE_PLAN``, default off), the
workspace's plan must have the ``llm_selector`` feature, and only the first
``max_llm_selector_contexts`` of its live contexts with the flag on, in the
order they were turned on (``llm_selector_enabled_at``, then the context id),
are active.

The shape of ``services.content_chunk_policy`` (#2103) on purpose, minus the
vector store predicate (the selector stores nothing, so there is no
``unsupported`` reason): the same rank, the same refusals (403 ``FEAT-001``,
429 ``QUOTA-001`` behind a per-workspace advisory lock), the same downgrade
report. Kept as a twin rather than a shared helper so either can change
without the other.

A plan that drops the feature, or a cap that shrinks, never changes the
stored flag: the earliest-enabled contexts stay active, the rest go
inactive, and an upgrade makes them active again.

Nothing here is cached across requests (#2061): a cached "off" would hide a
concurrent enable. With the gate off every function answers from the stored
flag alone and runs no plan query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, func, literal_column, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from config.plan_tiers import (
    PLAN_ORDER,
    PLAN_TIERS,
    PlanName,
    has_feature,
    plan_display_name,
    quota_gate_details,
)
from config.settings import get_settings
from models.auth import Context, Workspace
from models.config import ContextSearchConfig
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError
from utils.logger import get_logger

logger = get_logger(__name__)

#: The plan feature (``config.plan_tiers``).
LLM_SELECTOR_FEATURE = "llm_selector"

#: Why a context's selector is inactive, or why the flag cannot be turned on.
LLMSelectorReason = Literal["plan", "cap"]

# Namespaced like the other per-workspace advisory locks (``connector_seat:``,
# ``content_chunk_contexts:``); 64-bit ``hashtextextended`` (PR #686).
_CAP_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
# A row on without a stamp (none should exist) ranks last.
_NEVER = literal_column("'infinity'::timestamptz")


def llm_selector_gated() -> bool:
    """Whether this deployment gates the selector by plan (``LLM_SELECTOR_REQUIRE_PLAN``)."""
    return bool(get_settings().llm_selector_require_plan)


def plan_allows_llm_selector(plan_name: str | None) -> bool:
    """Whether ``plan_name`` has the ``llm_selector`` feature (``None`` reads as free)."""
    return has_feature(plan_name or PlanName.FREE, LLM_SELECTOR_FEATURE)


def llm_selector_context_limit(plan_name: str | None) -> int:
    """How many contexts of a workspace on ``plan_name`` may have the selector active.

    0 on a plan without the feature, whatever its ``max_llm_selector_contexts``.
    """
    if not plan_allows_llm_selector(plan_name):
        return 0
    tier = PLAN_TIERS.get(plan_name or PlanName.FREE)
    return max(0, int(tier.max_llm_selector_contexts)) if tier else 0


def _rank_key(config: Any) -> Any:
    """The order the cap keeps contexts in: earliest enable first, ties by context id."""
    return tuple_(func.coalesce(config.llm_selector_enabled_at, _NEVER), config.context_id)


@dataclass(frozen=True)
class LLMSelectorStatus:
    """One context's selector state.

    Attributes:
        stored: The stored ``llm_selector_enabled``.
        gated: The deployment gates the selector by plan.
        plan_name: The workspace's plan; ``None`` when not read.
        plan_has_feature: The plan has the ``llm_selector`` feature (True
            when not gated).
        limit: The plan's ``max_llm_selector_contexts`` (0 without the
            feature); ``None`` when not gated.
        in_use: Live contexts of the workspace with the flag stored on (this
            one included when it is on); ``None`` when not counted.
        earlier: Of those, how many rank before this one; ``None`` when not
            counted.
    """

    stored: bool
    gated: bool
    plan_name: str | None = None
    plan_has_feature: bool = True
    limit: int | None = None
    in_use: int | None = None
    earlier: int | None = None

    @property
    def effective(self) -> bool:
        """Recall acts on the flag for this context."""
        if not self.stored:
            return False
        if not self.gated:
            return True
        return (
            self.plan_has_feature
            and self.limit is not None
            and self.earlier is not None
            and self.earlier < self.limit
        )

    @property
    def can_enable(self) -> bool:
        """Turning the flag on would be accepted (the owner rule aside).

        Needs the counts (``include_usage``) when gated.
        """
        if not self.gated:
            return True
        return (
            self.plan_has_feature
            and self.limit is not None
            and self.in_use is not None
            and self.in_use < self.limit
        )

    @property
    def reason(self) -> LLMSelectorReason | None:
        """Why the selector is inactive (stored on) or cannot be turned on (stored off).

        ``None`` when nothing blocks.
        """
        blocked = not self.effective if self.stored else not self.can_enable
        if not blocked:
            return None
        if self.gated and not self.plan_has_feature:
            return "plan"
        return "cap"


async def _stored_flag(db: AsyncSession, context_id: Any) -> bool:
    if context_id is None:
        return False
    result = await db.execute(
        select(ContextSearchConfig.llm_selector_enabled).where(
            ContextSearchConfig.context_id == context_id
        )
    )
    return result.scalar_one_or_none() is True


def _usage_query(context_id: Any) -> Any:
    """The workspace, its plan and the cap counts for one context, in one SELECT."""
    mine = aliased(ContextSearchConfig, name="mine")
    other = aliased(ContextSearchConfig, name="other")
    other_ctx = aliased(Context, name="other_ctx")
    counted = func.count(other.context_id)
    return (
        select(
            Context.workspace_id,
            Workspace.plan_name,
            counted.label("in_use"),
            counted.filter(_rank_key(other) < _rank_key(mine)).label("earlier"),
        )
        .select_from(mine)
        .join(Context, Context.id == mine.context_id)
        .join(Workspace, Workspace.id == Context.workspace_id)
        .outerjoin(
            other_ctx,
            and_(
                other_ctx.workspace_id == Context.workspace_id,
                other_ctx.deleted_at.is_(None),
            ),
        )
        .outerjoin(
            other,
            and_(other.context_id == other_ctx.id, other.llm_selector_enabled.is_(True)),
        )
        .where(mine.context_id == context_id)
        .group_by(
            Context.workspace_id,
            Workspace.plan_name,
            mine.context_id,
            mine.llm_selector_enabled_at,
        )
    )


async def llm_selector_status(
    db: AsyncSession,
    context_id: Any,
    *,
    stored: bool | None = None,
    include_usage: bool = False,
) -> LLMSelectorStatus:
    """The context's selector state.

    Args:
        db: Session.
        context_id: The context.
        stored: The stored flag when the caller already read it (a loaded
            config row); ``None`` reads it.
        include_usage: Also count the workspace's contexts with the flag on
            (``in_use``), for a response or an enable. Without it the counts
            are read only when needed to answer ``effective``: gated and
            stored on.

    Returns:
        The status. With the gate off and no usage asked for, at most the one
        flag lookup.

    Raises:
        Exception: a failed lookup (callers that must not fail use
            :func:`llm_selector_effective`).
    """
    if stored is None:
        stored = await _stored_flag(db, context_id)
    gated = llm_selector_gated()
    if not include_usage and not (gated and stored):
        return LLMSelectorStatus(stored=stored, gated=gated)
    row = (await db.execute(_usage_query(context_id))).one_or_none()
    plan_name = row.plan_name if row is not None else None
    return LLMSelectorStatus(
        stored=stored,
        gated=gated,
        plan_name=plan_name,
        plan_has_feature=plan_allows_llm_selector(plan_name) if gated else True,
        limit=llm_selector_context_limit(plan_name) if gated else None,
        in_use=int(row.in_use or 0) if row is not None else 0,
        earlier=int(row.earlier or 0) if row is not None else None,
    )


async def llm_selector_effective(
    db: AsyncSession, context_id: Any, *, stored: bool | None = None
) -> bool:
    """Whether recall acts on the context's selector flag — THE flag for readers.

    Fails closed: a failed lookup answers False (no selector) and, run in a
    savepoint, leaves the caller's transaction usable.
    """
    if context_id is None or stored is False:
        return False
    if stored is True and not llm_selector_gated():
        return True
    try:
        async with db.begin_nested():
            return (await llm_selector_status(db, context_id, stored=stored)).effective
    except Exception as e:  # noqa: BLE001 — fail closed: no selector
        logger.warning(
            "llm_selector_status_load_failed",
            context_id=str(context_id),
            error_class=type(e).__name__,
        )
        return False


async def _workspace_of(db: AsyncSession, context_id: UUID) -> tuple[UUID | None, str | None]:
    row = (
        await db.execute(
            select(Context.workspace_id, Workspace.plan_name)
            .join(Workspace, Workspace.id == Context.workspace_id)
            .where(Context.id == context_id)
        )
    ).one_or_none()
    return (row.workspace_id, row.plan_name) if row is not None else (None, None)


async def ensure_llm_selector_plan(db: AsyncSession, context_id: UUID) -> None:
    """Refuse turning the flag on when the workspace's plan lacks the feature (gated only).

    Caller-independent: it runs before the owner rule, so an editor is not
    sent to an owner who would be refused too.

    Raises:
        FeatureNotAvailableError: 403 ``FEAT-001``, ``details.gate="plan"``.
    """
    if not llm_selector_gated():
        return
    _, plan_name = await _workspace_of(db, context_id)
    if not plan_allows_llm_selector(plan_name):
        raise FeatureNotAvailableError.for_feature(plan_name, "llm_selector")


async def _count_in_use(db: AsyncSession, workspace_id: UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(ContextSearchConfig)
        .join(Context, Context.id == ContextSearchConfig.context_id)
        .where(
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
            ContextSearchConfig.llm_selector_enabled.is_(True),
        )
    )
    return int(result.scalar_one() or 0)


async def _count_in_use_locked(db: AsyncSession, workspace_id: UUID) -> int:
    """``_count_in_use`` under the workspace's cap lock (held to the transaction end).

    The one place the lock key is built: every path that may turn a
    context's flag on counts through it. Lock order: a path that also takes
    the ``content_chunk_contexts:`` cap lock takes that one first (the
    search-config update runs the chunk gate before this one), so two
    requests never wait on each other's lock in reverse order.
    """
    await db.execute(_CAP_LOCK_SQL.bindparams(key=f"llm_selector_contexts:{workspace_id}"))
    return await _count_in_use(db, workspace_id)


def _lowest_plan_above(limit: int) -> str | None:
    """Lowest tier whose selector limit (feature included) exceeds ``limit``."""
    return next((name for name in PLAN_ORDER if llm_selector_context_limit(name) > limit), None)


def _raise_cap(plan_name: str | None, in_use: int, limit: int) -> None:
    required = _lowest_plan_above(limit)
    plan = plan_display_name(plan_name)
    if limit <= 0:
        message = f"The {plan} plan allows no contexts with the recall LLM selector."
        message += " Upgrade to turn it on." if required else ""
    else:
        upgrade = " or upgrade" if required else ""
        message = (
            f"The recall LLM selector is on in {in_use} of the {limit} context(s) the "
            f"{plan} plan allows. Turn it off in another context{upgrade}."
        )
    raise QuotaExceededError(
        message,
        **quota_gate_details(
            plan_name,
            "llm_selector_contexts",
            current=in_use,
            limit=limit,
            required_plan=required,
            feature="llm_selector",
        ),
    )


async def ensure_llm_selector_capacity(db: AsyncSession, context_id: UUID) -> None:
    """Refuse turning the flag on when the workspace is at its plan's cap (gated only).

    Takes the workspace's ``llm_selector_contexts:`` transaction advisory
    lock, then counts, so two enables at ``cap - 1`` cannot both pass: the
    second waits until the first commits and then counts it. Load-bearing
    (as in #1927): nothing between this call and the enable's commit
    (``ContextSearchConfigRepository.update``) may commit the session — a
    commit releases the lock. A plan whose cap is 0 is refused without the
    lock. A context whose stored flag is already on, read under the lock,
    passes: a repeated enable (a double click) whose first request committed
    meanwhile is one of the counted contexts, and the update keeps that first
    enable.

    Raises:
        QuotaExceededError: 429 ``QUOTA-001``,
            ``details.quota_type="llm_selector_contexts"``.
    """
    if not llm_selector_gated():
        return
    workspace_id, plan_name = await _workspace_of(db, context_id)
    if workspace_id is None:
        return
    limit = llm_selector_context_limit(plan_name)
    if limit <= 0:
        _raise_cap(plan_name, await _count_in_use(db, workspace_id), limit)
    in_use = await _count_in_use_locked(db, workspace_id)
    if in_use >= limit and not await _stored_flag(db, context_id):
        _raise_cap(plan_name, in_use, limit)


async def contexts_losing_llm_selector(
    db: AsyncSession, workspace_id: UUID, old_plan: str | None, new_plan: str | None
) -> list[UUID]:
    """The contexts whose active selector goes inactive when the workspace moves plans.

    The live contexts with the flag on, ranked as the cap ranks them, between
    the new plan's limit and the old one's (a plan without the feature has
    limit 0). Empty when the deployment does not gate. Writes nothing.
    """
    if not llm_selector_gated():
        return []
    old_limit = llm_selector_context_limit(old_plan)
    new_limit = llm_selector_context_limit(new_plan)
    if new_limit >= old_limit:
        return []
    result = await db.execute(
        select(ContextSearchConfig.context_id)
        .join(Context, Context.id == ContextSearchConfig.context_id)
        .where(
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
            ContextSearchConfig.llm_selector_enabled.is_(True),
        )
        .order_by(
            func.coalesce(ContextSearchConfig.llm_selector_enabled_at, _NEVER),
            ContextSearchConfig.context_id,
        )
        .offset(new_limit)
        .limit(old_limit - new_limit)
    )
    return list(result.scalars().all())
