"""Whether a context's content chunk vectors are active (#2103, #2080).

A context stores ``content_chunks_enabled`` (its owner's choice, #2015). Two
things decide whether that choice is acted on:

- the vector store: LanceDB keeps no chunk points (#2072);
- the plan, where the deployment gates it (``CONTENT_CHUNKS_REQUIRE_PLAN``,
  default off): the workspace's plan must have the ``content_chunks`` feature,
  and only the first ``max_content_chunk_contexts`` of its live contexts with
  the flag on, in the order they were turned on
  (``content_chunks_enabled_at``, then the context id), are active.

Two predicates, deliberately:

- **stored** (flag on and the store keeps chunk points): chunk points may
  exist. Metadata cascades and payload syncs follow it, so points kept while a
  context is inactive do not carry stale importance, tags or deletion when an
  upgrade brings them back.
- **effective** (stored and the plan allows it): chunk points are read and
  written. Recall, chunk writes, the Sleep rewrite, the backfill and the
  embedding migration's chunk rebuild follow it.

A plan that drops the feature, or a cap that shrinks, never changes the
stored flag and never purges points: the earliest-enabled contexts stay
active, the rest go inactive, and an upgrade makes them active again with
the points they kept. Nothing is re-queued when a context becomes active
again: a memory written while it was inactive has no chunk points, one whose
content was edited then lost them (they would hold the old text), and an
embedding migration run then wrote none into the new collection. Their
summary points still find them; their chunks come with the next content
edit or re-embed.

Nothing here is cached across requests (#2061): a cached "off" would hide a
concurrent enable. With the gate off every function answers from the flag and
the store alone, with the queries the flag read always took.
"""

from __future__ import annotations

from collections.abc import Iterable
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
from db.qdrant import chunk_points_supported
from models.auth import Context, Workspace
from models.config import ContextSearchConfig
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError
from utils.logger import get_logger

logger = get_logger(__name__)

#: The plan feature (``config.plan_tiers``).
CONTENT_CHUNKS_FEATURE = "content_chunks"
#: ``details.quota_type`` of the cap refusal (``config.constants.QUOTA_TYPES``).
CONTENT_CHUNK_CONTEXTS_QUOTA = "content_chunk_contexts"

#: Why a context's chunks are inactive, or why the flag cannot be turned on.
ContentChunksReason = Literal["unsupported", "plan", "cap"]

# Namespaced like the other per-workspace advisory locks (``connector_seat:``,
# ``resource_token_cap:``); 64-bit ``hashtextextended`` (PR #686).
_CAP_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
# A row on without a stamp (none should exist after f02) ranks last.
_NEVER = literal_column("'infinity'::timestamptz")


def content_chunks_gated() -> bool:
    """Whether this deployment gates content chunks by plan (``CONTENT_CHUNKS_REQUIRE_PLAN``)."""
    return bool(get_settings().content_chunks_require_plan)


def plan_allows_content_chunks(plan_name: str | None) -> bool:
    """Whether ``plan_name`` has the ``content_chunks`` feature (``None`` reads as free)."""
    return has_feature(plan_name or PlanName.FREE, CONTENT_CHUNKS_FEATURE)


def content_chunk_context_limit(plan_name: str | None) -> int:
    """How many contexts of a workspace on ``plan_name`` may have active chunks.

    0 on a plan without the feature, whatever its ``max_content_chunk_contexts``.
    """
    if not plan_allows_content_chunks(plan_name):
        return 0
    tier = PLAN_TIERS.get(plan_name or PlanName.FREE)
    return max(0, int(tier.max_content_chunk_contexts)) if tier else 0


def _rank_key(config: Any) -> Any:
    """The order the cap keeps contexts in: earliest enable first, ties by context id."""
    return tuple_(func.coalesce(config.content_chunks_enabled_at, _NEVER), config.context_id)


@dataclass(frozen=True)
class ContentChunksStatus:
    """One context's content chunk state.

    Attributes:
        stored: The stored ``content_chunks_enabled``.
        supported: The vector store keeps chunk points (not LanceDB).
        gated: The deployment gates content chunks by plan.
        plan_name: The workspace's plan; ``None`` when not read.
        plan_has_feature: The plan has the ``content_chunks`` feature (True
            when not gated).
        limit: The plan's ``max_content_chunk_contexts`` (0 without the
            feature); ``None`` when not gated.
        in_use: Live contexts of the workspace with the flag stored on (this
            one included when it is on); ``None`` when not counted.
        earlier: Of those, how many rank before this one; ``None`` when not
            counted.
    """

    stored: bool
    supported: bool
    gated: bool
    plan_name: str | None = None
    plan_has_feature: bool = True
    limit: int | None = None
    in_use: int | None = None
    earlier: int | None = None

    @property
    def has_points(self) -> bool:
        """Chunk points may exist: stored on, on a store that keeps them."""
        return self.stored and self.supported

    @property
    def effective(self) -> bool:
        """Chunk points are read and written for this context."""
        if not self.has_points:
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
        if not self.supported:
            return False
        if not self.gated:
            return True
        return (
            self.plan_has_feature
            and self.limit is not None
            and self.in_use is not None
            and self.in_use < self.limit
        )

    @property
    def reason(self) -> ContentChunksReason | None:
        """Why the chunks are inactive (stored on) or cannot be turned on (stored off).

        ``None`` when nothing blocks.
        """
        blocked = not self.effective if self.stored else not self.can_enable
        if not blocked:
            return None
        if not self.supported:
            return "unsupported"
        if self.gated and not self.plan_has_feature:
            return "plan"
        return "cap"


async def _stored_flag(db: AsyncSession, context_id: Any) -> bool:
    if context_id is None:
        return False
    result = await db.execute(
        select(ContextSearchConfig.content_chunks_enabled).where(
            ContextSearchConfig.context_id == context_id
        )
    )
    return result.scalar_one_or_none() is True


async def content_chunks_stored(db: AsyncSession, context_id: Any) -> bool:
    """Whether the context may hold chunk points: flag stored on, on a store that keeps them.

    One indexed lookup, never cached (#2061). Raises when the lookup fails.
    """
    return await _stored_flag(db, context_id) and chunk_points_supported()


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
            and_(other.context_id == other_ctx.id, other.content_chunks_enabled.is_(True)),
        )
        .where(mine.context_id == context_id)
        .group_by(
            Context.workspace_id,
            Workspace.plan_name,
            mine.context_id,
            mine.content_chunks_enabled_at,
        )
    )


async def content_chunks_status(
    db: AsyncSession,
    context_id: Any,
    *,
    stored: bool | None = None,
    include_usage: bool = False,
) -> ContentChunksStatus:
    """The context's content chunk state.

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
            :func:`content_chunks_effective`).
    """
    if stored is None:
        stored = await _stored_flag(db, context_id)
    supported = chunk_points_supported()
    gated = content_chunks_gated()
    if not include_usage and not (gated and stored):
        return ContentChunksStatus(stored=stored, supported=supported, gated=gated)
    row = (await db.execute(_usage_query(context_id))).one_or_none()
    plan_name = row.plan_name if row is not None else None
    return ContentChunksStatus(
        stored=stored,
        supported=supported,
        gated=gated,
        plan_name=plan_name,
        plan_has_feature=plan_allows_content_chunks(plan_name) if gated else True,
        limit=content_chunk_context_limit(plan_name) if gated else None,
        in_use=int(row.in_use or 0) if row is not None else 0,
        earlier=int(row.earlier or 0) if row is not None else None,
    )


async def content_chunks_effective(
    db: AsyncSession, context_id: Any, *, stored: bool | None = None
) -> bool:
    """Whether the context's chunk points are read and written — THE flag for readers (#2080).

    Fails closed: a failed lookup answers False (summary points only) and,
    run in a savepoint, leaves the caller's transaction usable.
    """
    if context_id is None or stored is False:
        return False
    if stored is True and not content_chunks_gated():
        return chunk_points_supported()
    try:
        async with db.begin_nested():
            return (await content_chunks_status(db, context_id, stored=stored)).effective
    except Exception as e:  # noqa: BLE001 — fail closed: no chunk points
        logger.warning(
            "content_chunks_status_load_failed",
            context_id=str(context_id),
            error_class=type(e).__name__,
        )
        return False


async def content_chunks_effective_ids(db: AsyncSession, context_ids: Iterable[UUID]) -> set[UUID]:
    """Those of ``context_ids`` whose chunks are active, in one SELECT (cross-context recall).

    Raises when the lookup fails; the caller decides (recall fails closed).
    """
    wanted = list(dict.fromkeys(context_ids))
    if not wanted or not chunk_points_supported():
        return set()
    if not content_chunks_gated():
        result = await db.execute(
            select(ContextSearchConfig.context_id).where(
                ContextSearchConfig.context_id.in_(wanted),
                ContextSearchConfig.content_chunks_enabled.is_(True),
            )
        )
        return set(result.scalars().all())
    ranked = (
        select(
            ContextSearchConfig.context_id,
            Workspace.plan_name,
            func.row_number()
            .over(
                partition_by=Context.workspace_id,
                order_by=[
                    func.coalesce(ContextSearchConfig.content_chunks_enabled_at, _NEVER),
                    ContextSearchConfig.context_id,
                ],
            )
            .label("rank"),
        )
        .join(Context, Context.id == ContextSearchConfig.context_id)
        .join(Workspace, Workspace.id == Context.workspace_id)
        .where(
            ContextSearchConfig.content_chunks_enabled.is_(True),
            Context.deleted_at.is_(None),
            Context.workspace_id.in_(select(Context.workspace_id).where(Context.id.in_(wanted))),
        )
        .subquery()
    )
    rows = await db.execute(
        select(ranked.c.context_id, ranked.c.plan_name, ranked.c.rank).where(
            ranked.c.context_id.in_(wanted)
        )
    )
    return {
        context_id
        for context_id, plan_name, rank in rows.all()
        if rank <= content_chunk_context_limit(plan_name)
    }


async def _workspace_of(db: AsyncSession, context_id: UUID) -> tuple[UUID | None, str | None]:
    row = (
        await db.execute(
            select(Context.workspace_id, Workspace.plan_name)
            .join(Workspace, Workspace.id == Context.workspace_id)
            .where(Context.id == context_id)
        )
    ).one_or_none()
    return (row.workspace_id, row.plan_name) if row is not None else (None, None)


async def ensure_content_chunks_plan(db: AsyncSession, context_id: UUID) -> None:
    """Refuse turning the flag on when the workspace's plan lacks the feature (gated only).

    Caller-independent, like the store check (#2072): it runs before the
    owner rule, so an editor is not sent to an owner who would be refused too.

    Raises:
        FeatureNotAvailableError: 403 ``FEAT-001``, ``details.gate="plan"``.
    """
    if not content_chunks_gated():
        return
    _, plan_name = await _workspace_of(db, context_id)
    if not plan_allows_content_chunks(plan_name):
        raise FeatureNotAvailableError.for_feature(plan_name, "content_chunks")


async def _count_in_use(db: AsyncSession, workspace_id: UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(ContextSearchConfig)
        .join(Context, Context.id == ContextSearchConfig.context_id)
        .where(
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
            ContextSearchConfig.content_chunks_enabled.is_(True),
        )
    )
    return int(result.scalar_one() or 0)


async def _count_in_use_locked(db: AsyncSession, workspace_id: UUID) -> int:
    """``_count_in_use`` under the workspace's cap lock (held to the transaction end).

    The one place the lock key is built: every path that may turn a
    context's flag on counts through it.
    """
    await db.execute(_CAP_LOCK_SQL.bindparams(key=f"content_chunk_contexts:{workspace_id}"))
    return await _count_in_use(db, workspace_id)


def _lowest_plan_above(limit: int) -> str | None:
    """Lowest tier whose content chunk limit (feature included) exceeds ``limit``."""
    return next((name for name in PLAN_ORDER if content_chunk_context_limit(name) > limit), None)


def _raise_cap(plan_name: str | None, in_use: int, limit: int) -> None:
    required = _lowest_plan_above(limit)
    plan = plan_display_name(plan_name)
    if limit <= 0:
        message = f"The {plan} plan allows no contexts with content chunk vectors."
        message += " Upgrade to turn them on." if required else ""
    else:
        upgrade = " or upgrade" if required else ""
        message = (
            f"Content chunk vectors are on in {in_use} of the {limit} context(s) the "
            f"{plan} plan allows. Turn them off in another context{upgrade}."
        )
    raise QuotaExceededError(
        message,
        **quota_gate_details(
            plan_name,
            "content_chunk_contexts",
            current=in_use,
            limit=limit,
            required_plan=required,
            feature="content_chunks",
        ),
    )


async def ensure_content_chunk_capacity(db: AsyncSession, context_id: UUID) -> None:
    """Refuse turning the flag on when the workspace is at its plan's cap (gated only).

    Takes the workspace's ``content_chunk_contexts:`` transaction advisory
    lock, then counts, so two enables at ``cap - 1`` cannot both pass: the
    second waits until the first commits and then counts it. Load-bearing
    (as in #1927): nothing between this call and the enable's commit
    (``ContextSearchConfigRepository.update``) may commit the session — a
    commit releases the lock. A plan whose cap is 0 is refused without the
    lock. A context whose stored flag is already on, read under the lock,
    passes: a repeated enable (a double click) whose first request committed
    meanwhile is one of the counted contexts, and the update keeps that first
    enable (v0.113.0 review chunks F2).

    Raises:
        QuotaExceededError: 429 ``QUOTA-001``,
            ``details.quota_type="content_chunk_contexts"``.
    """
    if not content_chunks_gated():
        return
    workspace_id, plan_name = await _workspace_of(db, context_id)
    if workspace_id is None:
        return
    limit = content_chunk_context_limit(plan_name)
    if limit <= 0:
        _raise_cap(plan_name, await _count_in_use(db, workspace_id), limit)
    in_use = await _count_in_use_locked(db, workspace_id)
    if in_use >= limit and not await _stored_flag(db, context_id):
        _raise_cap(plan_name, in_use, limit)


async def new_context_content_chunks(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    default_on: bool | None = None,
    resource_context: bool = False,
) -> bool:
    """``content_chunks_enabled`` for a NEW context of ``workspace_id``.

    ``DEFAULT_CONTENT_CHUNKS_ENABLED`` (or ``default_on``), and — where the
    deployment gates content chunks — only when turning the flag on would
    pass: the plan has the feature and the workspace is below its cap. The
    count is taken under the cap's advisory lock, so contexts created (or
    enabled) concurrently cannot together go past it; the lock is held until
    the caller's transaction ends, so the caller must insert the row before
    it commits. Without this a default-on deployment created every context
    stored-on and counted toward the cap (in use 3 of 1), inactive past it
    and never on a plan without the feature (v0.113.0 review chunks F1).

    A ``setup_resource`` context (``resource_context``) is created off under
    the gate: its indexer rows are never chunked, so the flag would only take
    one of the plan's slots from the owner's own contexts.
    """
    if default_on is None:
        default_on = bool(get_settings().default_content_chunks_enabled)
    if not default_on:
        return False
    if not content_chunks_gated():
        return True
    if resource_context:
        return False
    plan_name = (
        await db.execute(select(Workspace.plan_name).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    limit = content_chunk_context_limit(plan_name)
    if limit <= 0:
        return False
    return await _count_in_use_locked(db, workspace_id) < limit


async def contexts_losing_content_chunks(
    db: AsyncSession, workspace_id: UUID, old_plan: str | None, new_plan: str | None
) -> list[UUID]:
    """The contexts whose active chunks go inactive when the workspace moves plans.

    The live contexts with the flag on, ranked as the cap ranks them, between
    the new plan's limit and the old one's (a plan without the feature has
    limit 0). Empty when the deployment does not gate. Writes nothing.
    """
    if not content_chunks_gated():
        return []
    old_limit = content_chunk_context_limit(old_plan)
    new_limit = content_chunk_context_limit(new_plan)
    if new_limit >= old_limit:
        return []
    result = await db.execute(
        select(ContextSearchConfig.context_id)
        .join(Context, Context.id == ContextSearchConfig.context_id)
        .where(
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
            ContextSearchConfig.content_chunks_enabled.is_(True),
        )
        .order_by(
            func.coalesce(ContextSearchConfig.content_chunks_enabled_at, _NEVER),
            ContextSearchConfig.context_id,
        )
        .offset(new_limit)
        .limit(old_limit - new_limit)
    )
    return list(result.scalars().all())
