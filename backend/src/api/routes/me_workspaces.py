"""Current workspace membership and effective resource access for OIDC clients."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.oidc_bearer import OIDCPrincipal, identity_principal
from auth.workspace_roles import ContextRole, WorkspaceRole
from db.base import get_db
from models.auth import Workspace, WorkspaceMember
from services.capacity_lock import CapacityPolicy, capacity_lock_state
from services.permission_service import PermissionService
from services.plan_seats import context_writes_suspended
from utils.exceptions import (
    AuthorizationError,
    FeatureNotAvailableError,
    NotFoundException,
    ValidationError,
)
from utils.logger import get_logger

logger = get_logger(__name__)
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class IdentityInspectionRoute(APIRoute):
    """Keep dependency, validation and outage responses private and non-reflective."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap dependency resolution as well as the endpoint's normal response."""
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except HTTPException as exc:
                response = JSONResponse(
                    {"error": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )
            except (RequestValidationError, ValidationError):
                response = JSONResponse({"error": "invalid_request"}, status_code=400)
            except (AuthorizationError, NotFoundException, FeatureNotAvailableError):
                response = JSONResponse({"error": "not_found"}, status_code=404)
            except Exception as exc:
                # DB exceptions can contain connection strings or bound credentials.
                logger.warning("oidc_workspace_access_unavailable", failure_type=type(exc).__name__)
                response = JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
            response.headers.update(_NO_STORE)
            return response

        return handler


class InspectionError(BaseModel):
    """Fixed, non-reflective error envelope for identity inspection."""

    error: str


router = APIRouter(
    prefix="/me/workspaces",
    tags=["account-access"],
    route_class=IdentityInspectionRoute,
    responses={
        400: {"model": InspectionError, "description": "Invalid request"},
        401: {"model": InspectionError, "description": "Invalid identity access token"},
        403: {"model": InspectionError, "description": "Insufficient OIDC scope"},
        404: {"model": InspectionError, "description": "Not found or inaccessible"},
        429: {"model": InspectionError, "description": "Rate limited; honor Retry-After"},
        503: {"model": InspectionError, "description": "Dependency unavailable"},
        "default": {"model": InspectionError},
    },
)
Principal = Annotated[OIDCPrincipal, Depends(identity_principal)]
Database = Annotated[AsyncSession, Depends(get_db)]


class WorkspaceSummary(BaseModel):
    """One live membership belonging to the authenticated subject."""

    workspace_id: UUID
    name: str
    workspace_role: WorkspaceRole


class WorkspaceList(BaseModel):
    """Bounded, stable UUID-keyset page of memberships."""

    subject: str
    workspaces: list[WorkspaceSummary]
    next_cursor: UUID | None


class WorkspacePermissions(BaseModel):
    """Non-monetary inspection and canonical-owner contract authority."""

    billing_manage: bool = Field(serialization_alias="billing.manage")
    usage_read: bool = Field(serialization_alias="usage.read")


class ResourcePermissions(BaseModel):
    """Current content-read and synchronization role eligibility, not a write grant."""

    read: bool
    sync_manage: bool = Field(serialization_alias="sync.manage")


class ResourceAccess(BaseModel):
    """Effective access to a visible context in the explicitly named workspace."""

    context_id: UUID
    permissions: ResourcePermissions


class WorkspaceAccess(BaseModel):
    """Current authority, resolved from KMC records on each request."""

    subject: str
    workspace_id: UUID
    workspace_role: WorkspaceRole
    workspace_permissions: WorkspacePermissions
    resource: ResourceAccess | None


def _query(request: Request, allowed: set[str]) -> dict[str, str]:
    pairs = list(request.query_params.multi_items())
    if any(key not in allowed for key, _ in pairs) or len(dict(pairs)) != len(pairs):
        raise ValidationError("Invalid inspection request")
    return dict(pairs)


@router.get("", response_model=WorkspaceList)
async def list_workspaces(
    request: Request,
    principal: Principal,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: UUID | None = None,
) -> WorkspaceList:
    """List only the subject's live memberships, without consulting the UI workspace."""
    _query(request, {"limit", "cursor"})
    stmt = (
        select(Workspace.id, Workspace.name, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == principal.user_id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.id)
        .limit(limit + 1)
    )
    if cursor is not None:
        stmt = stmt.where(Workspace.id > cursor)
    rows = (await db.execute(stmt)).all()
    page = rows[:limit]
    return WorkspaceList(
        subject=principal.user_id,
        workspaces=[
            WorkspaceSummary(workspace_id=wid, name=name, workspace_role=role)
            for wid, name, role in page
        ],
        next_cursor=page[-1][0] if len(rows) > limit else None,
    )


@router.get("/{workspace_id}/access", response_model=WorkspaceAccess)
async def workspace_access(
    workspace_id: UUID,
    request: Request,
    principal: Principal,
    db: Database,
    context_id: UUID | None = None,
) -> WorkspaceAccess:
    """Inspect membership and optional resource eligibility with the existing RBAC gates."""
    _query(request, {"context_id"})
    wid = workspace_id
    permissions = PermissionService(db)
    member = await permissions.check_workspace_access(
        principal.user_id, wid, required_role=WorkspaceRole.VIEWER, require_seat=False
    )
    workspace = await db.get(Workspace, wid)
    if workspace is None:
        raise NotFoundException("Workspace")
    resource = None
    if context_id is not None:
        context, role = await permissions.check_context_access(
            principal.user_id,
            context_id,
            capacity=CapacityPolicy.ALLOWED,
            key_workspace_id=wid,
        )
        readable = await capacity_lock_state(db, workspace) is None
        writable = (
            readable
            and role in (ContextRole.EDITOR, ContextRole.OWNER)
            and not await context_writes_suspended(db, workspace, context)
        )
        resource = ResourceAccess(
            context_id=context.id,
            permissions=ResourcePermissions(read=readable, sync_manage=writable),
        )
    return WorkspaceAccess(
        subject=principal.user_id,
        workspace_id=wid,
        workspace_role=member.role,
        workspace_permissions=WorkspacePermissions(
            billing_manage=workspace.owner_user_id == principal.user_id
            and member.role == WorkspaceRole.OWNER,
            usage_read=True,
        ),
        resource=resource,
    )
