"""Seats and writable slots when a plan's cap drops below what a workspace holds (#1943).

Unlike the #1939 suspensions in :mod:`services.plan_suspension` (an effective
limit falling to zero), these caps stay POSITIVE: the members beyond the seats
and the contexts beyond the writable slots are refused one by one, ranked so
the owner and the oldest contexts always keep theirs. Each decision needs a
query; nothing is stored — every check re-reads the workspace's effective
limits at use time, so re-subscribing (or removing members / contexts)
restores everything on the next call.

Layering: ``config.plan_tiers`` <- ``services.plan_seats`` <-
``services.plan_suspension``. This module never imports a ``services.*``
module — the permission, context and workspace services import it.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from auth.workspace_roles import WorkspaceRole
from config.plan_tiers import paused_details, paused_message
from models.auth import Context, Workspace, WorkspaceMember
from utils.exceptions import FeatureNotAvailableError
from utils.logger import get_logger

logger = get_logger(__name__)

MEMBERS_LIMIT_ATTR: Final = "max_members_per_workspace"
CONTEXTS_LIMIT_ATTR: Final = "max_contexts_per_workspace"


def seat_order() -> tuple[Any, ...]:
    """Owner first, then earliest join (``invited_at`` for a legacy row), then id.

    "Owner" is the ``owner`` role or the workspace's ``owner_user_id`` — the
    same two tests ``member_seat_suspended`` short-circuits on, so the ranking,
    the refusal and the ``seat_paused`` flags can never disagree.
    """
    owner_of_workspace = (
        select(Workspace.owner_user_id)
        .where(Workspace.id == WorkspaceMember.workspace_id)
        .scalar_subquery()
    )
    return (
        case(
            (
                or_(
                    WorkspaceMember.role == WorkspaceRole.OWNER,
                    WorkspaceMember.user_id == owner_of_workspace,
                ),
                0,
            ),
            else_=1,
        ),
        func.coalesce(WorkspaceMember.joined_at, WorkspaceMember.invited_at).asc().nulls_last(),
        WorkspaceMember.id.asc(),
    )


def seat_limit(workspace: Workspace) -> int:
    """The seats the plan gives this workspace — at least one; the owner always sits.

    Args:
        workspace: The workspace whose effective member cap is read.

    Returns:
        ``max(effective_max_members, 1)``.
    """
    return max(workspace.effective_max_members, 1)


async def member_seat_suspended(
    db: AsyncSession, workspace: Workspace, member: WorkspaceMember
) -> bool:
    """Whether ``member`` has no seat on the workspace's plan (#1943).

    The owner always has one (no query). Otherwise one scalar read decides:
    is ``member`` among the first :func:`seat_limit` rows in
    :func:`seat_order` — also under a one-seat plan, where the seat goes to
    whoever ranks first (the owner when it holds a membership row), the same
    answer the members list gives.
    """
    if member.role == WorkspaceRole.OWNER or member.user_id == workspace.owner_user_id:
        return False
    seated = (
        select(WorkspaceMember.id)
        .where(WorkspaceMember.workspace_id == workspace.id)
        .order_by(*seat_order())
        .limit(seat_limit(workspace))
        .subquery()
    )
    has_seat = await db.scalar(select(exists().where(seated.c.id == member.id)))
    return not has_seat


async def _member_count(db: AsyncSession, workspace_id: UUID) -> int:
    return int(
        await db.scalar(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
        or 0
    )


async def ensure_member_seated(
    db: AsyncSession, workspace: Workspace | UUID, member: WorkspaceMember
) -> None:
    """Refuse a member without a seat (``FEAT-001`` ``members``, #1943).

    Called AFTER membership and role are established, so a non-member keeps
    its uniform refusal and the seat never answers for someone else's
    workspace. The refusal names the tier that would seat everyone.

    Raises:
        FeatureNotAvailableError: 403 ``FEAT-001`` with ``feature="members"``,
            ``limit`` (the seats) and ``current`` (the members).
    """
    if member.role == WorkspaceRole.OWNER:
        return
    if not isinstance(workspace, Workspace):
        # The id form (the resolvers' RBAC core, ContextService, MCP): loaded
        # here, after the owner short-circuit, so the owner costs no query.
        loaded = await db.get(Workspace, workspace)
        if loaded is None:
            return
        workspace = loaded
    if not await member_seat_suspended(db, workspace, member):
        return
    count = await _member_count(db, workspace.id)
    limit = seat_limit(workspace)
    raise FeatureNotAvailableError(
        paused_message(workspace.plan_name, "members", MEMBERS_LIMIT_ATTR, above=count - 1),
        **paused_details(
            workspace.plan_name,
            "members",
            MEMBERS_LIMIT_ATTR,
            above=count - 1,
            limit=limit,
            current=count,
        ),
    )


def live_contexts(workspace_id: UUID) -> ColumnElement[bool]:
    """The workspace's live (not soft-deleted) contexts, as a WHERE clause.

    Args:
        workspace_id: The workspace whose contexts are matched.

    Returns:
        A clause for ``Context`` rows of that workspace with no ``deleted_at``.
    """
    return and_(Context.workspace_id == workspace_id, Context.deleted_at.is_(None))


def slot_limit(workspace: Workspace) -> int:
    """The writable context slots the plan gives this workspace (never negative).

    Args:
        workspace: The workspace whose effective context cap is read.

    Returns:
        ``max(effective_max_contexts, 0)``.
    """
    return max(workspace.effective_max_contexts, 0)


async def context_writes_suspended(
    db: AsyncSession, workspace: Workspace, context: Context
) -> bool:
    """Whether ``context`` has no writable slot on the workspace's plan (#1943).

    The oldest ``effective_max_contexts`` live contexts (``created_at``, then
    ``id``) keep their slot; one indexed COUNT of the older live contexts
    decides. A deleted context frees its slot for the next one.
    """
    limit = slot_limit(workspace)
    if limit <= 0:
        return True
    older = await db.scalar(
        select(func.count(Context.id)).where(
            live_contexts(workspace.id),
            or_(
                Context.created_at < context.created_at,
                and_(Context.created_at == context.created_at, Context.id < context.id),
            ),
        )
    )
    return int(older or 0) >= limit


async def writes_paused_context_ids(db: AsyncSession, workspace: Workspace) -> set[UUID]:
    """The live contexts beyond the writable slots, oldest-first ranking."""
    limit = slot_limit(workspace)
    rows = await db.scalars(
        select(Context.id)
        .where(live_contexts(workspace.id))
        .order_by(Context.created_at.asc(), Context.id.asc())
        .offset(limit)
    )
    return set(rows.all())


async def ensure_context_writable(db: AsyncSession, context: Context | UUID | str) -> None:
    """Refuse a content write into a context without a slot (``FEAT-001`` ``contexts``).

    Called after the caller's write access to the context is established and
    after the #1941 capacity lock. Reads, listing, export, delete and metadata
    updates never call this. ``context`` may be the row (the resolvers) or its
    id (the service's write paths); an unknown id passes — the caller's own
    not-found handling owns that case.

    Raises:
        FeatureNotAvailableError: 403 ``FEAT-001`` with ``feature="contexts"``,
            ``limit`` (the slots) and ``current`` (the live contexts).
    """
    if not isinstance(context, Context):
        try:
            context_id = context if isinstance(context, UUID) else UUID(str(context))
        except (ValueError, TypeError):
            # The caller resolved the context already; an unparsable id here is
            # a programming error upstream, not a reason to refuse — but say so.
            logger.warning("context_slot_check_skipped_unparsable_id", context=str(context))
            return
        loaded = await db.get(Context, context_id)
        if loaded is None:
            return
        context = loaded
    workspace = await db.get(Workspace, context.workspace_id)
    if workspace is None or not await context_writes_suspended(db, workspace, context):
        return
    count = int(
        await db.scalar(select(func.count(Context.id)).where(live_contexts(workspace.id))) or 0
    )
    limit = slot_limit(workspace)
    raise FeatureNotAvailableError(
        paused_message(workspace.plan_name, "contexts", CONTEXTS_LIMIT_ATTR, above=count - 1),
        **paused_details(
            workspace.plan_name,
            "contexts",
            CONTEXTS_LIMIT_ATTR,
            above=count - 1,
            limit=limit,
            current=count,
        ),
    )
