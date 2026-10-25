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

The #1943 seats and writable slots (a POSITIVE cap below what the workspace
holds, refused one by one) live in :mod:`services.plan_seats`; this module
keeps the ``suspended`` wire vocabulary and reports them in
:func:`suspended_features`.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.resource_tokens import workspace_regular_active_tokens
from config.plan_tiers import paused_details, paused_message
from models.auth import Context, Workspace, WorkspaceMember
from models.resource import ResourceToken, WorkspaceConnector
from services.plan_seats import live_contexts, seat_limit, slot_limit
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


async def connector_key_workspace(
    db: AsyncSession, connector_id: UUID, workspace_id: UUID
) -> Workspace | None:
    """The workspace a connector's write key is checked against (#1944, #1952).

    One query: the workspace row joined through its connector. The id is the
    key's ``api_keys.connector_id``. ``None`` when that id resolves to no
    connector OF THE KEY'S WORKSPACE — the FK and the mint-time workspace check
    make that reachable only with tampered data, and the caller refuses the
    key (fail closed, like the agent kill switch). Otherwise the caller pauses
    the key while :func:`connectors_suspended`.

    Reads the plan as the session knows it: like every ``effective_*`` reader,
    this expects the per-request session (a ``Workspace`` already in the
    identity map, with its cached tier, is not re-read).

    Args:
        db: Database session.
        connector_id: The key's ``api_keys.connector_id``.
        workspace_id: The key's own workspace scope.

    Returns:
        The key's workspace when the connector belongs to it, else ``None``.
    """
    result = await db.execute(
        select(Workspace)
        .join(WorkspaceConnector, WorkspaceConnector.workspace_id == Workspace.id)
        .where(
            WorkspaceConnector.id == connector_id,
            WorkspaceConnector.workspace_id == workspace_id,
        )
    )
    return result.scalar_one_or_none()


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
    live = live_contexts(workspace_id)
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
        db, select(Context.id).where(live, Context.sleep_mode != "skip")
    ):
        suspended.append(SUSPENDED_SLEEP)
    if public_suspended(workspace) and await _exists(
        db, select(Context.id).where(live, Context.is_public.is_(True))
    ):
        suspended.append(SUSPENDED_PUBLIC)
    # #1943: over the seats / slots — "is there a row beyond the limit", as an
    # EXISTS over an OFFSET, so this stays a presence check like the others.
    if await _exists(
        db,
        select(WorkspaceMember.id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .offset(seat_limit(workspace)),
    ):
        suspended.append(SUSPENDED_MEMBERS)
    if await _exists(
        db,
        select(Context.id).where(live).offset(slot_limit(workspace)),
    ):
        suspended.append(SUSPENDED_CONTEXTS)
    return suspended
