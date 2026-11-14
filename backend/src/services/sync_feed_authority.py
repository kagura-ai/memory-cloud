"""Current human data authority and exact-target fences for feed operations."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from auth.sync_identity import SyncPrincipal, load_sync_identity
from auth.workspace_roles import ContextRole
from config.settings import get_settings
from db.base import Base
from models.auth import Context, ContextMember, User, Workspace, WorkspaceMember
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource, ResourceSchema
from models.sync_installation import SyncInstallation, SyncService
from services.capacity_lock import CapacityPolicy, ensure_not_capacity_locked
from services.permission_service import PermissionService
from services.plan_seats import ensure_context_writable
from services.workspace_locks import lock_workspace_for_update
from utils.datetime import utcnow
from utils.db_helpers import is_lock_not_available
from utils.exceptions import ConflictError, NotFoundException

T = TypeVar("T", bound=Base)


async def locked(
    db: AsyncSession, model: type[T], *conditions: Any, write: bool = False
) -> T | None:
    """Nonblocking authority locks, always refreshing the identity map."""
    try:
        return await db.scalar(
            select(model)
            .where(*conditions)
            .with_for_update(read=not write, nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        if is_lock_not_available(exc):
            raise ConflictError("Synchronization state is changing; retry") from exc
        raise


def runs_enabled() -> None:
    """Fail closed before exposing the new surface and again immediately at commit."""
    settings = get_settings()
    if not (
        settings.oidc_enabled and settings.sync_integrations_enabled and settings.sync_runs_enabled
    ):
        raise NotFoundException("Synchronization runs")


@dataclass
class FeedFence:
    """Retain finite deadlines through the surrounding transaction commit."""

    deadlines: list[datetime] = field(default_factory=list)

    def check(self, _session: Session) -> None:
        runs_enabled()
        if any(utcnow() >= deadline for deadline in self.deadlines):
            raise ConflictError("Synchronization preparation expired")


@asynccontextmanager
async def feed_fence(db: AsyncSession) -> AsyncIterator[FeedFence]:
    """Enter outside the paid guard so this fence also covers its final commit."""
    runs_enabled()
    fence = FeedFence()
    event.listen(db.sync_session, "before_commit", fence.check)
    try:
        yield fence
    finally:
        event.remove(db.sync_session, "before_commit", fence.check)


async def exact_target(db: AsyncSession, row: SyncInstallation) -> None:
    """Never rebind missing or drifted UUID targets, including private contexts."""
    resource = await locked(db, Resource, Resource.id == row.resource_pk, write=True)
    context = await locked(db, Context, Context.id == row.context_id, write=True)
    endpoint = await locked(db, PublicMcpEndpoint, PublicMcpEndpoint.id == row.endpoint_id)
    if (
        resource is None
        or context is None
        or endpoint is None
        or resource.workspace_id != row.workspace_id
        or resource.resource_id != row.resource_id
        or context.workspace_id != row.workspace_id
        or context.resource_id != row.resource_id
        or context.deleted_at is not None
        or context.is_private
        or not context.is_public
        or endpoint.workspace_id != row.workspace_id
        or endpoint.resource_pk != row.resource_pk
        or endpoint.context_id != row.context_id
        or endpoint.resource_id != row.resource_id
        or endpoint.revoked_at is not None
    ):
        raise NotFoundException("Synchronization target")


async def lock_membership(
    db: AsyncSession, user_id: str, row: SyncInstallation, *, write: bool
) -> None:
    """Fence membership deletion, role/reach changes and context grants before RBAC."""
    user = await locked(db, User, User.user_id == user_id)
    member = await locked(
        db,
        WorkspaceMember,
        WorkspaceMember.workspace_id == row.workspace_id,
        WorkspaceMember.user_id == user_id,
    )
    await locked(
        db,
        ContextMember,
        ContextMember.context_id == row.context_id,
        ContextMember.user_id == user_id,
    )
    if user is None or member is None:
        raise NotFoundException("Synchronization target")
    await PermissionService(db).check_context_access(
        user_id,
        row.context_id,
        required_role=ContextRole.EDITOR if write else ContextRole.VIEWER,
        capacity=CapacityPolicy.BLOCKED,
        key_workspace_id=row.workspace_id,
    )


@asynccontextmanager
async def human_feed_boundary(
    db: AsyncSession, wid: UUID, iid: UUID, principal: SyncPrincipal, *, write: bool = False
) -> AsyncIterator[tuple[SyncInstallation, Workspace, FeedFence]]:
    """OIDC proves a human; current normal data permissions prove each operation."""
    async with feed_fence(db) as fence:
        try:
            await locked(db, User, User.user_id == principal.user_id)
            workspace = await lock_workspace_for_update(db, wid, nowait=True)
            live = await load_sync_identity(
                db,
                principal.token_hash,
                "integrations:manage" if write else "sync.records:read",
                lock=True,
            )
            if (live.user_id, live.client_id) != (principal.user_id, principal.client_id):
                raise NotFoundException("Synchronization target")
            row = await locked(
                db,
                SyncInstallation,
                SyncInstallation.id == iid,
                SyncInstallation.workspace_id == wid,
            )
            if row is None or row.state == "revoked" or row.revoked_at is not None:
                raise NotFoundException("Synchronization target")
            service = await locked(db, SyncService, SyncService.service_id == row.service_id)
            if (
                service is None
                or not service.enabled
                or service.browser_client_id != live.client_id
            ):
                raise NotFoundException("Synchronization target")
            await exact_target(db, row)
            await lock_membership(db, live.user_id, row, write=write)
            fence.deadlines.append(live.expires_at)
            yield row, workspace, fence
        except BaseException:
            await db.rollback()
            raise


async def current_schema(db: AsyncSession, row: SyncInstallation) -> ResourceSchema | None:
    """Resource UPDATE blocks a concurrent schema insertion through its FK."""
    return await db.scalar(
        select(ResourceSchema)
        .where(ResourceSchema.resource_pk == row.resource_pk)
        .order_by(ResourceSchema.schema_version.desc())
        .limit(1)
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )


async def worker_target(
    db: AsyncSession, row: SyncInstallation, *, content_write: bool = False
) -> None:
    """Called only after the paid service guard owns the Workspace lock."""
    if not db.info.get("paid_integration_write"):
        raise RuntimeError("Synchronization writes require paid authority")
    await exact_target(db, row)
    if content_write:
        await ensure_not_capacity_locked(db, row.workspace_id)
        context = await db.get(Context, row.context_id)
        if context is None:
            raise NotFoundException("Synchronization target")
        await ensure_context_writable(db, context)
