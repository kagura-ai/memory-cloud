"""Which public MCP endpoint is served, re-checked on every request (#2675).

``load_serving_endpoint`` is the one gate the anonymous transport runs before
anything else: kill switch, then one SELECT joining the endpoint, its context,
its resource and its workspace. Every failure — unknown id, unpublished,
context no longer public, slug moved, resource gone, workspace deleted, switch
off — is the same ``None``, so the caller answers one indistinguishable 404.
Nothing is cached: unpublishing takes effect on the next request, including
on a session opened before it.

``load_serving_endpoint_for_alias`` is the same SELECT for a verified custom
domain (#2809): joined to its ``active`` domain row whose lease is current.
Both entries resolve to the same ``ServingEndpoint``. A name that is no longer
claimable (an operator reserved it after activation) is ``None`` before any
statement, so every reader of this answer (the request path, the permission
endpoint, the owner's ``serving``) agrees with the Host guard.

``check_publishable`` backs the owner's publish route with the same
preconditions, as typed errors naming the one that failed (the owner may see
why).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Row, Select, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from mcp_server.public.domains import claimable_hostname, public_domains_enabled
from models.auth import Context, Workspace
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_domain import PublicMcpDomain
from models.resource import Resource
from services.capacity_lock import ensure_not_capacity_locked
from services.plan_suspension import ensure_public_serving_allowed
from utils.exceptions import (
    CapacityLockedError,
    NotFoundException,
    QuotaExceededError,
    ValidationError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# The database clock, the only clock of a lease decision.
_DB_NOW = literal_column("timezone('UTC', statement_timestamp())")


@dataclass(frozen=True)
class ServingEndpoint:
    """A published endpoint whose preconditions held when it was loaded.

    The identity fields (``id`` … ``resource_id``) are what #2678 / #2679 key
    on. ``workspace`` is the loaded row, used only for the pause / capacity
    checks; it never leaves the server.
    """

    id: UUID
    public_id: str
    workspace_id: UUID
    context_id: UUID
    resource_pk: UUID
    resource_id: str
    public_name: str
    public_description: str | None
    public_usage_guide: str | None
    context_name: str
    owner_user_id: str
    workspace: Workspace | None = field(default=None, compare=False, repr=False)


class PublicationPreconditionError(ValidationError):
    """A resource cannot be published (422); ``precondition`` names why."""

    def __init__(self, message: str, *, precondition: str) -> None:
        super().__init__(message, field="resource_id", precondition=precondition)


def public_endpoints_enabled() -> bool:
    """Whether the operator switched anonymous public endpoints on."""
    return bool(get_settings().mcp_public_endpoints_enabled)


def _serving_select() -> Select[PublicMcpEndpoint, str, Workspace]:
    """The serving SELECT without an address: joins and preconditions only.

    The endpoint, its context (same workspace, same slug), its resource row
    (the bound one, same workspace, same slug) and its workspace; the
    endpoint not revoked, the context live, public and not private, the
    workspace not deleted. Both entries add only their own address to it.
    """
    return (
        select(PublicMcpEndpoint, Context.name, Workspace)
        .join(
            Context,
            (Context.id == PublicMcpEndpoint.context_id)
            & (Context.workspace_id == PublicMcpEndpoint.workspace_id)
            & (Context.resource_id == PublicMcpEndpoint.resource_id),
        )
        .join(
            Resource,
            (Resource.id == PublicMcpEndpoint.resource_pk)
            & (Resource.workspace_id == PublicMcpEndpoint.workspace_id)
            & (Resource.resource_id == PublicMcpEndpoint.resource_id),
        )
        .join(Workspace, Workspace.id == PublicMcpEndpoint.workspace_id)
        .where(
            PublicMcpEndpoint.revoked_at.is_(None),
            Context.deleted_at.is_(None),
            Context.is_public.is_(True),
            Context.is_private.is_(False),
            Workspace.deleted_at.is_(None),
        )
    )


def _serving_endpoint(row: Row[PublicMcpEndpoint, str, Workspace]) -> ServingEndpoint:
    """The ``ServingEndpoint`` of one row of ``_serving_select()``."""
    endpoint, context_name, workspace = row
    return ServingEndpoint(
        id=endpoint.id,
        public_id=endpoint.public_id,
        workspace_id=endpoint.workspace_id,
        context_id=endpoint.context_id,
        resource_pk=endpoint.resource_pk,
        resource_id=endpoint.resource_id,
        public_name=endpoint.public_name,
        public_description=endpoint.public_description,
        public_usage_guide=endpoint.public_usage_guide,
        context_name=context_name,
        owner_user_id=workspace.owner_user_id,
        workspace=workspace,
    )


async def load_serving_endpoint(db: AsyncSession, public_id: str) -> ServingEndpoint | None:
    """Return the endpoint served at ``public_id``, or ``None`` (one 404 for all).

    Args:
        db: Session.
        public_id: The address segment (``pmcp_…``).

    Returns:
        The endpoint when the switch is on, it is not revoked, its context is
        live, public, not private and still bound to the same slug, its
        resource row is the bound one, and its workspace is not deleted.
    """
    if not public_endpoints_enabled():
        return None
    row = (
        await db.execute(_serving_select().where(PublicMcpEndpoint.public_id == public_id))
    ).first()
    if row is None:
        logger.debug("public_mcp_not_published", public_id=public_id)
        return None
    return _serving_endpoint(row)


async def load_serving_endpoint_for_alias(
    db: AsyncSession, hostname: str
) -> ServingEndpoint | None:
    """Return the endpoint a verified custom domain serves, or ``None`` (#2809).

    The same SELECT as ``load_serving_endpoint`` joined to the domain row of
    the same endpoint and workspace, which must be ``active`` with its lease
    (``serve_until``) still ahead of the database clock. The lease predicate
    is the same under both authority settings. One query, no cache; the host
    name is never logged. A name ``claimable_hostname`` refuses (malformed,
    or reserved since its row was activated) is ``None`` without a statement,
    whatever its row says: the job marks such a row ``suspended(reserved)``
    at its next check.

    Args:
        db: Session.
        hostname: The normalised, claimable host name of the request.

    Returns:
        The endpoint when both switches are on and the alias is served; the
        same ``ServingEndpoint`` the fixed address loads.
    """
    if not public_endpoints_enabled() or not public_domains_enabled():
        return None
    if claimable_hostname(hostname) != hostname:
        return None
    statement = (
        _serving_select()
        .join(
            PublicMcpDomain,
            (PublicMcpDomain.endpoint_id == PublicMcpEndpoint.id)
            & (PublicMcpDomain.workspace_id == PublicMcpEndpoint.workspace_id),
        )
        .where(
            PublicMcpDomain.hostname == hostname,
            PublicMcpDomain.state == "active",
            PublicMcpDomain.serve_until > _DB_NOW,
        )
    )
    row = (await db.execute(statement)).first()
    if row is None:
        logger.debug("public_mcp_alias_not_served")
        return None
    return _serving_endpoint(row)


async def serving_paused(db: AsyncSession, endpoint: ServingEndpoint) -> bool:
    """Whether the workspace's plan pause or capacity lock stops serving now.

    Only these two refusals are caught; their messages (plan, quota, counts)
    are not forwarded — the caller answers a bare 503.
    """
    workspace = endpoint.workspace or await db.get(Workspace, endpoint.workspace_id)
    if workspace is None:
        return True
    try:
        ensure_public_serving_allowed(workspace)
        await ensure_not_capacity_locked(db, workspace, outsider=True)
    except (QuotaExceededError, CapacityLockedError):
        return True
    return False


async def check_publishable(
    db: AsyncSession, workspace_id: UUID, resource_id: str
) -> tuple[Context, Resource]:
    """Return the context and resource a publication of ``resource_id`` binds.

    Args:
        db: Session.
        workspace_id: The owner's workspace.
        resource_id: The resource slug.

    Returns:
        ``(context, resource)``.

    Raises:
        NotFoundException: No resource with that slug in the workspace.
        PublicationPreconditionError: No live context is bound to the
            resource (``context_bound``), or it is not public
            (``context_public``).
    """
    resource = (
        await db.execute(
            select(Resource).where(
                Resource.workspace_id == workspace_id, Resource.resource_id == resource_id
            )
        )
    ).scalar_one_or_none()
    if resource is None:
        raise NotFoundException("Resource", resource_id)
    context = (
        await db.execute(
            select(Context).where(
                Context.workspace_id == workspace_id,
                Context.resource_id == resource_id,
                Context.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if context is None:
        raise PublicationPreconditionError(
            "No live context is bound to this resource; run setup_resource first.",
            precondition="context_bound",
        )
    if not context.is_public or context.is_private:
        raise PublicationPreconditionError(
            "The resource's context is not public; set is_public=true on it first.",
            precondition="context_public",
        )
    return context, resource
