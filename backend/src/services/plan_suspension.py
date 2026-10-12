"""Suspend paid-only features on a workspace whose plan dropped them (#1939).

A workspace that falls back to Free keeps every object it made on a paid plan
(connectors, resource tokens, Sleep-enabled contexts, public contexts) — data
is never deleted on a downgrade. What stops is the paid-only WORK those objects
drive: ingestion, Sleep's LLM passes, anonymous public serving. Re-subscribing
lifts the suspension with no further action, because nothing is stored: every
gate re-reads the workspace's effective limits when it runs.

The predicates test the EFFECTIVE limit dropping to ``<= 0``, never
``has_feature``. Feature flags gate *creation* only; Basic and Pro keep
positive serve-only limits so objects made on a higher plan keep serving
(#1551), and a flag check here would stop those too.

#1943 adds a second kind of pause: when a plan's cap is POSITIVE but below what
the workspace holds, the members beyond the seats and the contexts beyond the
writable slots are refused one by one (ranked, so the owner and the oldest
contexts always keep theirs). Unlike the four predicates above these need a
query; the same "nothing stored, re-read at use time" stance applies.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from auth.resource_tokens import workspace_regular_active_tokens
from auth.workspace_roles import WorkspaceRole
from models.auth import Context, Workspace, WorkspaceMember
from models.resource import ResourceToken, WorkspaceConnector
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError
from utils.logger import get_logger

logger = get_logger(__name__)

# Wire vocabulary of ``GET /workspaces/{id}/plan`` ``suspended`` — the web UI
# keys its copy on these strings, so they never change spelling.
SUSPENDED_CONNECTORS: Final = "connectors"
SUSPENDED_RESOURCES: Final = "resources"
SUSPENDED_SLEEP: Final = "sleep"
SUSPENDED_PUBLIC: Final = "public"
# #1943: members without a seat and contexts without a writable slot.
SUSPENDED_MEMBERS: Final = "members"
SUSPENDED_CONTEXTS: Final = "contexts"
SUSPENDABLE_FEATURES: Final = (
    SUSPENDED_CONNECTORS,
    SUSPENDED_RESOURCES,
    SUSPENDED_SLEEP,
    SUSPENDED_PUBLIC,
    SUSPENDED_MEMBERS,
    SUSPENDED_CONTEXTS,
)


def connectors_suspended(workspace: Workspace) -> bool:
    """True when the workspace has no connector seats (connector ingest stops)."""
    return workspace.effective_max_connectors <= 0


async def connectors_suspended_for(db: AsyncSession, workspace_id: UUID) -> bool:
    """:func:`connectors_suspended` for a workspace known only by id (#1939).

    A missing workspace row reads as not suspended — the caller's own lookup
    (a connector row, a key row) already decided the target exists.
    """
    workspace = await db.get(Workspace, workspace_id)
    return workspace is not None and connectors_suspended(workspace)


async def connector_key_suspended(db: AsyncSession, connector_id: UUID, workspace_id: UUID) -> bool:
    """Whether a ``connector:{id}`` API key is paused with its connector (#1944).

    One query: the workspace row joined through its connector. A name that
    matches no connector OF THIS WORKSPACE resolves to ``False`` — the key is
    an ordinary workspace key — without a second lookup.

    Reads the plan as the session knows it: like every ``effective_*`` reader,
    this expects the per-request session (a ``Workspace`` already in the
    identity map, with its cached tier, is not re-read).

    Args:
        db: Database session.
        connector_id: The id parsed from the key name.
        workspace_id: The key's own workspace scope.

    Returns:
        True when the connector exists in the workspace and the workspace has
        no connector seats (:func:`connectors_suspended`).
    """
    result = await db.execute(
        select(Workspace)
        .join(WorkspaceConnector, WorkspaceConnector.workspace_id == Workspace.id)
        .where(
            WorkspaceConnector.id == connector_id,
            WorkspaceConnector.workspace_id == workspace_id,
        )
    )
    workspace = result.scalar_one_or_none()
    return workspace is not None and connectors_suspended(workspace)


def resources_suspended(workspace: Workspace) -> bool:
    """True when the workspace has no resource-token allowance (resource ingest stops)."""
    return workspace.effective_max_resource_tokens <= 0


def sleep_suspended(workspace: Workspace) -> bool:
    """True when the workspace may not run Sleep on any context."""
    return workspace.effective_sleep_enabled_contexts_limit <= 0


def public_suspended(workspace: Workspace) -> bool:
    """True when the workspace has no public-call allowance (public serving stops).

    One exception: a tier whose feature set includes ``public_contexts`` (a
    self-hosted ``PLAN_<KEY>_FEATURES`` override; ``public_calls_per_day`` has
    no env override) can create public contexts, so pausing what it may
    create would be wrong. This is the only feature-flag read in the module,
    and it can only lift a pause, never cause one (#1551 still holds).
    """
    if workspace.effective_public_calls_per_day > 0:
        return False
    from config.plan_tiers import has_feature

    return not has_feature(workspace.plan_name, "public_contexts")


async def _is_connector_resource(db: AsyncSession, resource_pk: UUID | None) -> bool:
    """Whether ``resource_pk`` is owned by a connector (UNIQUE ``resource_pk``).

    The same split ``workspace_regular_active_tokens`` makes: a connector-owned
    resource is gated by connector seats, every other one by the resource-token
    allowance. An unresolved resource (``None``) is an ordinary one.
    """
    if resource_pk is None:
        return False
    found = await db.execute(
        select(WorkspaceConnector.id).where(WorkspaceConnector.resource_pk == resource_pk).limit(1)
    )
    return found.scalar_one_or_none() is not None


async def _suspended_ingest_feature(
    db: AsyncSession, workspace: Workspace, resource_pk: UUID | None
) -> str | None:
    """The suspended feature that blocks ingest into ``resource_pk``, if any."""
    connectors_off = connectors_suspended(workspace)
    resources_off = resources_suspended(workspace)
    # The common case — a paid plan — answers without a query.
    if not connectors_off and not resources_off:
        return None
    if await _is_connector_resource(db, resource_pk):
        return SUSPENDED_CONNECTORS if connectors_off else None
    return SUSPENDED_RESOURCES if resources_off else None


async def ingest_suspended(
    db: AsyncSession, workspace: Workspace, resource_pk: UUID | None
) -> bool:
    """Non-raising twin of :func:`ensure_ingest_allowed`, for background jobs.

    Args:
        db: Database session.
        workspace: The resource's workspace.
        resource_pk: ``resources.id`` being ingested into, or ``None``.

    Returns:
        True when ingest into that resource is suspended on this plan.
    """
    return await _suspended_ingest_feature(db, workspace, resource_pk) is not None


async def ensure_ingest_allowed(
    db: AsyncSession, workspace: Workspace, resource_pk: UUID | None
) -> None:
    """Refuse resource / connector ingest on a plan that no longer carries it.

    Args:
        db: Database session.
        workspace: The resource's workspace (already authorized by the caller).
        resource_pk: ``resources.id`` being ingested into, or ``None``.

    Raises:
        FeatureNotAvailableError: ``FEAT-001`` (403, gate ``plan``) naming
            ``connectors`` for a connector-owned resource or ``resources`` for
            any other, so clients show the upgrade path.
    """
    feature = await _suspended_ingest_feature(db, workspace, resource_pk)
    if feature == SUSPENDED_CONNECTORS:
        raise FeatureNotAvailableError(
            paused_message(workspace.plan_name, "connectors", "max_connectors"),
            **paused_details(workspace.plan_name, "connectors", "max_connectors"),
        )
    if feature == SUSPENDED_RESOURCES:
        raise FeatureNotAvailableError(
            paused_message(workspace.plan_name, "resources", "max_resource_tokens"),
            **paused_details(workspace.plan_name, "resources", "max_resource_tokens"),
        )


def paused_details(
    plan_name: str | None, feature: str, limit_attr: str, *, above: int = 0, **extra: Any
) -> dict[str, Any]:
    """``FEAT-001`` details for a paused feature, naming the tier that RESUMES it.

    ``FeatureNotAvailableError.for_feature`` names the tier that can CREATE
    the feature (the registry minimum, e.g. XL for connectors), but existing
    objects resume on any tier whose limit is above zero (#1551 serve-only),
    which is usually a lower one. The upgrade path therefore comes from the
    numeric cap, like the other numeric refusals.

    Args:
        plan_name: The workspace's plan key.
        feature: Registry feature key (``connectors`` / ``resources``).
        limit_attr: ``PlanTier`` field whose positive value resumes it.
        above: The value the resuming tier must exceed — ``0`` for a feature
            that is simply off; ``count - 1`` for a cap the workspace is over
            (#1943: the tier that seats everyone / makes every context writable).
        extra: Further ``details`` keys (e.g. ``limit`` / ``current``).

    Returns:
        ``gate`` / ``feature`` / ``required_plan`` / ``required_plan_display``
        / ``current_plan`` (+ ``extra``), ready to splat into the exception.
    """
    from config.plan_tiers import PLAN_TIERS, feature_gate_details, lowest_tier_with_limit

    required = lowest_tier_with_limit(limit_attr, above)
    tier = PLAN_TIERS.get(required) if required else None
    details = feature_gate_details(plan_name, feature)
    details["required_plan"] = required
    details["required_plan_display"] = tier.display_name if tier else None
    details.update(extra)
    return details


def paused_message(plan_name: str | None, feature: str, limit_attr: str, *, above: int = 0) -> str:
    """Refusal text for a paused feature; names the tier that resumes it.

    Args:
        plan_name: The workspace's plan key.
        feature: Registry feature key.
        limit_attr: ``PlanTier`` field whose positive value resumes it.
        above: See :func:`paused_details`.

    Returns:
        The message.
    """
    from config.plan_tiers import PLAN_TIERS, lowest_tier_with_limit, plan_display_name

    required = lowest_tier_with_limit(limit_attr, above)
    message = f"Feature '{feature}' is paused on {plan_display_name(plan_name)} plan."
    if required is not None:
        message += f" Upgrade to {PLAN_TIERS[required].display_name} plan to resume it."
    return message


# ---------------------------------------------------------------------------
# #1943: seats and writable slots
# ---------------------------------------------------------------------------

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
    """The seats the plan gives this workspace — at least one; the owner always sits."""
    return _seat_limit(workspace, None)


def _seat_limit(workspace: Workspace, limit: int | None) -> int:
    return max(workspace.effective_max_members if limit is None else limit, 1)


async def seated_member_ids(
    db: AsyncSession, workspace: Workspace, *, limit: int | None = None
) -> set[int]:
    """The ``WorkspaceMember.id`` of every member who keeps a seat."""
    rows = await db.scalars(
        select(WorkspaceMember.id)
        .where(WorkspaceMember.workspace_id == workspace.id)
        .order_by(*seat_order())
        .limit(_seat_limit(workspace, limit))
    )
    return set(rows.all())


async def seat_paused_member_ids(
    db: AsyncSession, workspace: Workspace, *, limit: int | None = None
) -> set[int]:
    """The members who have no seat under the (effective or given) limit."""
    seated = await seated_member_ids(db, workspace, limit=limit)
    everyone = await db.scalars(
        select(WorkspaceMember.id).where(WorkspaceMember.workspace_id == workspace.id)
    )
    return set(everyone.all()) - seated


async def member_seat_suspended(
    db: AsyncSession, workspace: Workspace, member: WorkspaceMember
) -> bool:
    """Whether ``member`` has no seat on the workspace's plan (#1943).

    The owner always has one (no query). Otherwise one ranked SELECT decides —
    also under a one-seat plan, where the seat goes to whoever ranks first
    (the owner when it holds a membership row), the same answer
    ``seat_paused_member_ids`` and the members list give.
    """
    if member.role == WorkspaceRole.OWNER or member.user_id == workspace.owner_user_id:
        return False
    return member.id not in await seated_member_ids(db, workspace)


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
    limit = _seat_limit(workspace, None)
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


def _live_contexts(workspace_id: UUID) -> ColumnElement[bool]:
    return and_(Context.workspace_id == workspace_id, Context.deleted_at.is_(None))


async def context_writes_suspended(
    db: AsyncSession, workspace: Workspace, context: Context
) -> bool:
    """Whether ``context`` has no writable slot on the workspace's plan (#1943).

    The oldest ``effective_max_contexts`` live contexts (``created_at``, then
    ``id``) keep their slot; one indexed COUNT of the older live contexts
    decides. A deleted context frees its slot for the next one.
    """
    limit = max(workspace.effective_max_contexts, 0)
    if limit <= 0:
        return True
    older = await db.scalar(
        select(func.count(Context.id)).where(
            _live_contexts(workspace.id),
            or_(
                Context.created_at < context.created_at,
                and_(Context.created_at == context.created_at, Context.id < context.id),
            ),
        )
    )
    return int(older or 0) >= limit


async def writes_paused_context_ids(db: AsyncSession, workspace: Workspace) -> set[UUID]:
    """The live contexts beyond the writable slots, oldest-first ranking."""
    limit = max(workspace.effective_max_contexts, 0)
    rows = await db.scalars(
        select(Context.id)
        .where(_live_contexts(workspace.id))
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
        await db.scalar(select(func.count(Context.id)).where(_live_contexts(workspace.id))) or 0
    )
    limit = max(workspace.effective_max_contexts, 0)
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


def ensure_public_serving_allowed(workspace: Workspace) -> None:
    """Refuse anonymous public serving on a plan with no public-call allowance.

    For anonymous callers only: signed-in callers are metered by the
    rate-limit middleware on their own plan, which answers the same
    ``api_public_daily`` refusal at a zero allowance (no counts — the
    rate-limit family has none to ship). The context keeps ``is_public``;
    re-subscribing serves it again.

    Args:
        workspace: The public context's workspace.

    Raises:
        QuotaExceededError: 429 with ``quota_type`` ``api_public_daily``.
    """
    if public_suspended(workspace):
        raise QuotaExceededError(
            "Public access to this context is paused: the workspace's plan has no "
            "public API allowance.",
            quota_type="api_public_daily",
        )


async def _exists(db: AsyncSession, stmt: Select[Any]) -> bool:
    """True when ``stmt`` matches at least one row.

    Runs ``SELECT EXISTS (stmt)``, which stops at the first row instead of
    counting them all — the owner's layout asks on every page load.
    """
    return bool((await db.execute(select(stmt.exists()))).scalar())


async def suspended_features(db: AsyncSession, workspace: Workspace) -> list[str]:
    """Paid-only features currently paused on this workspace, in a stable order.

    A feature is listed only when it is suspended AND the workspace still has
    something it would drive — a Free workspace that never had a connector is
    not told its connectors are paused. Only suspended features are queried,
    so the four paid-only features cost a paid workspace nothing; the seat and
    slot checks (#1943) are two indexed presence reads on every plan.

    Args:
        db: Database session.
        workspace: The workspace to report on.

    Returns:
        A subset of :data:`SUSPENDABLE_FEATURES`, in that order.
    """
    workspace_id = workspace.id
    live_contexts = (Context.workspace_id == workspace_id, Context.deleted_at.is_(None))
    suspended: list[str] = []
    if connectors_suspended(workspace) and await _exists(
        db,
        select(WorkspaceConnector.id).where(WorkspaceConnector.workspace_id == workspace_id),
    ):
        suspended.append(SUSPENDED_CONNECTORS)
    if resources_suspended(workspace) and await _exists(
        db, workspace_regular_active_tokens(workspace_id, ResourceToken.id)
    ):
        suspended.append(SUSPENDED_RESOURCES)
    if sleep_suspended(workspace) and await _exists(
        db, select(Context.id).where(*live_contexts, Context.sleep_mode != "skip")
    ):
        suspended.append(SUSPENDED_SLEEP)
    if public_suspended(workspace) and await _exists(
        db, select(Context.id).where(*live_contexts, Context.is_public.is_(True))
    ):
        suspended.append(SUSPENDED_PUBLIC)
    # #1943: over the seats / slots — "is there a row beyond the limit", as an
    # EXISTS over an OFFSET, so this stays a presence check like the others.
    if await _exists(
        db,
        select(WorkspaceMember.id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .offset(_seat_limit(workspace, None)),
    ):
        suspended.append(SUSPENDED_MEMBERS)
    if await _exists(
        db,
        select(Context.id).where(*live_contexts).offset(max(workspace.effective_max_contexts, 0)),
    ):
        suspended.append(SUSPENDED_CONTEXTS)
    return suspended
