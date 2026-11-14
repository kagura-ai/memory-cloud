"""Atomic dedicated-target creation and exact, non-rebinding target inspection."""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import EMBEDDING_MODEL_REGISTRY
from config.plan_tiers import has_feature
from config.settings import get_settings
from mcp_server.public.publication import public_endpoints_enabled
from models.auth import Context, Workspace
from models.config import ContextSearchConfig
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from models.sync_installation import SyncAuthorization, SyncInstallation
from models.sync_installation_schemas import SyncInstallationView
from repositories.config_repository import new_search_config_row
from services.capacity_lock import ensure_not_capacity_locked
from services.content_chunk_policy import new_context_content_chunks
from services.quota_service import QuotaService
from utils.exceptions import ConflictError, FeatureNotAvailableError


async def create_sync_target(
    db: AsyncSession, workspace: Workspace, approval: SyncAuthorization
) -> tuple[Resource, Context, PublicMcpEndpoint]:
    """Create new identities under the caller's Workspace lock, without a commit."""
    if not has_feature(workspace.plan_name, "resources"):
        raise FeatureNotAvailableError.for_feature(workspace.plan_name, "resources")
    await ensure_not_capacity_locked(db, workspace)
    await QuotaService(db).check_context_creation_allowed(workspace.id, raise_on_denied=True)
    if not public_endpoints_enabled():
        raise ConflictError("Public MCP endpoints are not enabled on this deployment")
    rid, cid, eid = uuid4(), uuid4(), uuid4()
    slug = "sync-" + rid.hex
    resource = Resource(
        id=rid,
        workspace_id=workspace.id,
        resource_id=slug,
        name=approval.public_name,
        created_by=approval.authorized_by,
    )
    db.add(resource)
    await db.flush()
    context = Context(
        id=cid,
        workspace_id=workspace.id,
        name=slug,
        display_name=approval.public_name,
        created_by=approval.authorized_by,
        is_private=False,
        is_public=True,
        resource_id=slug,
    )
    db.add(context)
    await db.flush()
    settings = get_settings()
    model = settings.embedding_model
    dimensions = EMBEDDING_MODEL_REGISTRY.get(model, (settings.embedding_dimensions,))[0]
    chunks = await new_context_content_chunks(db, workspace.id, resource_context=True)
    db.add(
        ContextSearchConfig(
            context_id=cid,
            semantic_weight=0.6,
            fetch_factor=3,
            embedding_model=model,
            embedding_dimensions=dimensions,
            **new_search_config_row(settings, content_chunks_enabled=chunks),
        )
    )
    endpoint = PublicMcpEndpoint(
        id=eid,
        workspace_id=workspace.id,
        context_id=cid,
        resource_pk=rid,
        resource_id=slug,
        public_name=approval.public_name,
        public_description=approval.public_description,
        public_usage_guide=approval.public_usage_guide,
        created_by=approval.authorized_by,
    )
    db.add(endpoint)
    await db.flush()
    return resource, context, endpoint


async def installation_view(db: AsyncSession, row: SyncInstallation) -> SyncInstallationView:
    """Inspect the exact UUID/workspace/slug binding; never repair missing targets."""
    endpoint = await db.scalar(
        select(PublicMcpEndpoint)
        .join(
            Context,
            (Context.id == PublicMcpEndpoint.context_id)
            & (Context.workspace_id == row.workspace_id)
            & (Context.resource_id == row.resource_id),
        )
        .join(
            Resource,
            (Resource.id == PublicMcpEndpoint.resource_pk)
            & (Resource.workspace_id == row.workspace_id)
            & (Resource.resource_id == row.resource_id),
        )
        .where(
            PublicMcpEndpoint.id == row.endpoint_id,
            PublicMcpEndpoint.workspace_id == row.workspace_id,
            PublicMcpEndpoint.context_id == row.context_id,
            PublicMcpEndpoint.resource_pk == row.resource_pk,
            PublicMcpEndpoint.resource_id == row.resource_id,
            PublicMcpEndpoint.revoked_at.is_(None),
            Context.deleted_at.is_(None),
            Context.is_public.is_(True),
            Context.is_private.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    return SyncInstallationView(
        installation_id=row.id,
        workspace_id=row.workspace_id,
        service_id=row.service_id,
        service_client_id=row.service_client_id,
        application_id=row.application_id,
        resource_pk=row.resource_pk,
        context_id=row.context_id,
        resource_id=row.resource_id,
        endpoint_id=row.endpoint_id,
        public_id=endpoint.public_id if endpoint else None,
        public_path=f"/mcp/public/{endpoint.public_id}" if endpoint else None,
        target_available=endpoint is not None,
        state=row.state,
        generation=row.generation,
        operations=row.operations,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )
