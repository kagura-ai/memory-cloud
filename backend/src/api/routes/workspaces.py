"""Workspace API Routes.

Issue #115 Phase B-3: Workspace-level Multi-tenancy

Provides REST API for workspace CRUD and member management.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.analysis_allowlist import check_workspace_in_allowlist
from auth.dependencies import SessionUser, get_current_user, require_byok_enabled
from auth.workspace_roles import WorkspaceRole
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import Context, ExternalAPIKey, User, Workspace
from models.schemas import OpenAIKeyStatusResponse
from services.email_service import get_email_service
from services.permission_service import PermissionService
from services.workspace_ownership_service import WorkspaceOwnershipService
from services.workspace_service import WorkspaceService
from utils.auth_helpers import get_user_id
from utils.datetime import to_utc_iso
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["workspaces"])


# ============================================================================
# Request/Response Models
# ============================================================================


class WorkspaceCreate(BaseModel):
    """Request model for creating workspace.

    Issue #146: OpenAI API key is now optional (can be added later).
    Issue #169: Default context settings (summary, usage_guide, embedding_model).
    Issue #276: Slug removed - not used for routing, simplified UX.
    """

    name: str = Field(..., min_length=1, max_length=255)
    openai_api_key: str | None = Field(None, pattern=r"^sk-[A-Za-z0-9_-]+$", min_length=20)
    description: str | None = None
    # Issue #169: Default context settings
    default_context_name: str | None = Field(None, pattern=r"^[a-z0-9_-]+$", max_length=100)
    default_context_summary: str | None = Field(None, max_length=500)
    default_context_usage_guide: str | None = Field(None, max_length=2000)
    default_context_embedding_model: str | None = Field(
        None, pattern=r"^(text-embedding-3-small|text-embedding-3-large)$"
    )


class WorkspaceUpdate(BaseModel):
    """Request model for updating workspace.

    Issue #276: Slug removed.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None


class WorkspaceResponse(BaseModel):
    """Response model for workspace.

    Issue #276: Slug removed.
    """

    id: UUID
    name: str
    description: str | None
    owner_user_id: str
    plan_name: str
    member_count: int
    context_count: int
    created_at: str
    current_user_role: str | None = None  # Current user's role in this workspace
    analyses_enabled: bool = False  # Memory broadlistening allowlist (#497)

    class Config:
        from_attributes = True


class CredentialsStatusInfo(BaseModel):
    """Credentials status information for member."""

    api_key_count: int = 0
    api_key_visible: bool = False
    claude_app_visible: bool | None = None  # None = not created
    chatgpt_app_visible: bool | None = None
    custom_app_count: int = 0


class ContextStatsItem(TZAwareBaseModel):
    """Context usage statistics item.

    Issue #249: Per-context statistics for workspace overview.
    """

    context_id: str
    context_name: str
    memory_count: int
    last_activity: datetime | None
    member_count: int
    api_calls_week: int = 0
    active_users_week: int = 0
    avg_response_time_ms: float = 0.0


class WorkspaceTotals(BaseModel):
    """Workspace-wide totals.

    Issue #249: Summary totals for all contexts.
    """

    memory_count: int


class WorkspaceContextStatsResponse(BaseModel):
    """Response model for context statistics.

    Issue #249: GET /api/v1/workspaces/{workspace_id}/contexts/stats
    """

    contexts: list[ContextStatsItem]
    total_contexts: int
    workspace_totals: WorkspaceTotals


class DailyUsageItem(BaseModel):
    """Daily usage statistics item.

    Issue #249: Time-series usage data for context.
    """

    date: str
    api_calls: int
    unique_users: int


class UserActivityItem(TZAwareBaseModel):
    """User activity statistics item.

    Issue #249: Per-user activity breakdown.
    """

    user_id: str
    user_name: str | None
    user_email: str | None
    api_calls: int
    last_activity: datetime | None


class ContextUsageTimelineResponse(BaseModel):
    """Response model for context usage timeline.

    Issue #249: GET /api/v1/workspaces/{workspace_id}/contexts/{context_id}/usage-timeline
    """

    context_id: str
    context_name: str
    daily_usage: list[DailyUsageItem]
    total_calls: int


class ContextUserActivityResponse(BaseModel):
    """Response model for context user activity.

    Issue #249: GET /api/v1/workspaces/{workspace_id}/contexts/{context_id}/user-activity
    """

    context_id: str
    context_name: str
    users: list[UserActivityItem]
    total_users: int


class DailyMemoryCount(BaseModel):
    """Daily memory count item for timeline.

    Issue #275 Task 6: Memory timeline visualization.
    """

    date: str
    count: int


class MemoryTimelineResponse(BaseModel):
    """Response model for workspace memory timeline.

    Issue #275 Task 6: GET /api/v1/workspaces/{workspace_id}/memory-timeline
    """

    workspace_id: str
    workspace_name: str
    daily_counts: list[DailyMemoryCount]
    memories_created_in_period: int  # Renamed for clarity
    period_start: str
    period_end: str


class TimelineItem(BaseModel):
    """Timeline data point.

    Issue #265: P1-6 - Type-safe timeline.
    """

    date: str
    count: int


class SearchTimelineItem(BaseModel):
    """Search timeline data point with anonymous/authenticated split.

    Issue #265: P1-6 - Type-safe timeline.
    """

    date: str
    total: int
    anonymous: int
    authenticated: int


class ResourceIngestStats(BaseModel):
    """Resource Ingest API statistics.

    Issue #265: Public API usage stats.
    """

    total_events: int
    last_n_days: int
    avg_per_day: float
    active_tokens: int
    timeline: list[TimelineItem]  # P1-6: Type-safe


class PublicSearchStats(BaseModel):
    """Public Search API statistics.

    Issue #265: Public API usage stats.
    """

    total_searches: int
    last_n_days: int
    anonymous: int
    authenticated: int
    timeline: list[SearchTimelineItem]  # P1-6: Type-safe


class PublicAPIStatsResponse(BaseModel):
    """Response model for public API statistics.

    Issue #265: GET /api/v1/workspaces/{workspace_id}/contexts/{context_id}/public-api-stats
    """

    resource_ingest: ResourceIngestStats
    public_search: PublicSearchStats


class WorkspaceMemberResponse(BaseModel):
    """Response model for workspace member."""

    user_id: str
    user_name: str | None = None
    user_email: str | None = None
    role: str
    joined_at: str | None
    credentials_status: CredentialsStatusInfo | None = None  # New: credentials info

    # User activity fields
    last_login_at: str | None = None
    # Issue #246: current_context_id removed

    # Issue #234: Context access restriction
    allowed_context_ids: list[str] | None = None

    class Config:
        from_attributes = True


class AddMemberRequest(BaseModel):
    """Request model for adding member."""

    user_id: str = Field(..., min_length=1)
    role: str = Field(..., pattern=r"^(owner|admin|member|viewer)$")


class UpdateMemberRoleRequest(BaseModel):
    """Request model for updating member role."""

    role: str = Field(..., pattern=r"^(owner|admin|member|viewer)$")


class UpdateMemberContextAccessRequest(BaseModel):
    """Request model for updating member's context access.

    Issue #234: Context access restriction for member/viewer.
    """

    allowed_context_ids: list[str] | None = Field(
        None,
        description=(
            "List of context IDs the member can access. "
            "null = no restriction (all contexts). "
            "[] = no context access. "
            "[uuid1, uuid2, ...] = only these contexts."
        ),
    )


# ============================================================================
# Workspace CRUD Endpoints
# ============================================================================


@router.post("", response_model=WorkspaceResponse, status_code=201)
async def create_workspace(
    body: WorkspaceCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a new workspace.

    The authenticated user becomes the workspace owner.

    Issue #149: Plan tier enforcement - check multi-workspace restrictions.
    """
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)

    # Issue #149: Check if user can create another workspace
    from services.quota_service import QuotaService

    quota_service = QuotaService(db)
    can_create, error = await quota_service.check_workspace_creation_allowed(
        user["user_id"], raise_on_denied=True
    )

    # Issue #169: Pass default context settings
    workspace = await workspace_service.create_workspace(
        name=body.name,
        owner_user_id=user["user_id"],
        openai_api_key=body.openai_api_key,
        description=body.description,
        default_context_name=body.default_context_name,
        default_context_summary=body.default_context_summary,
        default_context_usage_guide=body.default_context_usage_guide,
        default_context_embedding_model=body.default_context_embedding_model,
    )

    # Get member and context counts
    stats = await workspace_service.get_workspace_stats(workspace.id)

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        owner_user_id=workspace.owner_user_id,
        plan_name=workspace.plan_name,
        member_count=stats["member_count"],
        context_count=stats["context_count"],
        created_at=to_utc_iso(workspace.created_at) or "",
        analyses_enabled=check_workspace_in_allowlist(workspace.id),
    )


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all workspaces user belongs to."""
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)

    workspaces = await workspace_service.list_user_workspaces(user["user_id"])

    # Get stats and user role for each workspace
    result = []
    for workspace in workspaces:
        stats = await workspace_service.get_workspace_stats(workspace.id)

        # Get current user's role in this workspace
        member = await workspace_service.get_member(
            workspace.id, user["user_id"], raise_if_not_found=False
        )
        user_role: str | None = member.role if member else None

        result.append(
            WorkspaceResponse(
                id=workspace.id,
                name=workspace.name,
                description=workspace.description,
                owner_user_id=workspace.owner_user_id,
                plan_name=workspace.plan_name,
                member_count=stats["member_count"],
                context_count=stats["context_count"],
                created_at=to_utc_iso(workspace.created_at) or "",
                current_user_role=user_role,
                analyses_enabled=check_workspace_in_allowlist(workspace.id),
            )
        )

    return result


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Get workspace details."""
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # Check access
    await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.MEMBER
    )

    workspace = await workspace_service.get_workspace(workspace_id)
    stats = await workspace_service.get_workspace_stats(workspace_id)

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        owner_user_id=workspace.owner_user_id,
        plan_name=workspace.plan_name,
        member_count=stats["member_count"],
        context_count=stats["context_count"],
        created_at=to_utc_iso(workspace.created_at) or "",
        analyses_enabled=check_workspace_in_allowlist(workspace.id),
    )


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: UUID,
    body: WorkspaceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update workspace details (name, description).

    Issue #276: Slug removed.

    Requires owner role.
    """
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # Check owner access (Settings is owner-only)
    await perm_service.check_workspace_owner(user["user_id"], workspace_id)

    workspace = await workspace_service.update_workspace(
        workspace_id=workspace_id,
        name=body.name,
        description=body.description,
    )

    stats = await workspace_service.get_workspace_stats(workspace_id)

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        owner_user_id=workspace.owner_user_id,
        plan_name=workspace.plan_name,
        member_count=stats["member_count"],
        context_count=stats["context_count"],
        created_at=to_utc_iso(workspace.created_at) or "",
        analyses_enabled=check_workspace_in_allowlist(workspace.id),
    )


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Delete workspace (soft delete).

    Requires owner role.
    """
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check for destructive operation
    # Issue #269: check_workspace_owner verifies ownership (sufficient for security)
    # Additional owner_user_id check for extra safety on destructive operations
    await perm_service.check_workspace_owner(user["user_id"], workspace_id)

    # Double verification for destructive operation
    from models.auth import Workspace

    workspace_result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = workspace_result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if workspace.owner_user_id != user["user_id"]:
        raise HTTPException(
            status_code=403, detail="Only the workspace owner can delete the workspace."
        )

    await workspace_service.delete_workspace(workspace_id, deleted_by=user["user_id"])


@router.put("/{workspace_id}/switch")
async def switch_workspace(
    workspace_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Switch to a different workspace.

    Updates the user's current_workspace_id to the specified workspace.
    Requires the user to have at least viewer access to the workspace.
    """
    user = await get_current_user(request)
    perm_service = PermissionService(db)

    # Verify the user can access this workspace. VIEWER is the floor on purpose:
    # the workspace switcher lists every workspace the user belongs to (incl.
    # viewer-role memberships), and switching only sets current_workspace_id — a
    # mutable UI preference that is never trusted for authorization (see
    # PermissionService.resolve_resource_by_slug contract). A viewer who can
    # already read the workspace must be able to enter it; requiring MEMBER here
    # left viewers listed-but-unenterable (403 role_too_low). All real access is
    # still enforced per-operation downstream.
    await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.VIEWER
    )

    # Update user's current workspace
    user_result = await db.execute(select(User).where(User.user_id == user["user_id"]))
    user_record = user_result.scalar_one_or_none()

    if user_record:
        user_record.current_workspace_id = workspace_id
        # Issue #246: current_context_id assignment removed (column deleted)

        await db.commit()
        logger.info(
            "org_switched_context_not_assigned",
            user_id=user["user_id"],
            workspace_id=str(workspace_id),
            reason="User must explicitly choose context for security",
        )

    return {"status": "ok", "workspace_id": str(workspace_id)}


# ============================================================================
# Workspace Member Management Endpoints
# ============================================================================


@router.get("/{workspace_id}/members", response_model=list[WorkspaceMemberResponse])
async def list_members(
    workspace_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all members of workspace."""
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # Check access
    await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.MEMBER
    )

    members = await workspace_service.list_members(workspace_id)

    # Fetch user details + credentials status for each member
    from sqlalchemy import and_, func

    from models.auth import APIKey, OAuth2Client

    member_responses = []
    for m in members:
        # Get user details from database
        user_result = await db.execute(select(User).where(User.user_id == m.user_id))
        user_record = user_result.scalar_one_or_none()

        # Issue #246: current_context_id removed - skip context lookup
        # Get current context if user has one
        # if user_record and user_record.current_context_id:
        #     context_result = await db.execute(
        #         select(Context.name, Context.display_name, Context.is_private, Context.created_by)
        #         .where(
        #             Context.id == user_record.current_context_id,
        #             Context.deleted_at.is_(None)
        #         )
        #     )
        #     context = context_result.one_or_none()
        #     if context:
        #         # Issue #165: Privacy check - only show context name if:
        #         # 1. Context is shared (is_private=False), OR
        #         # 2. Viewing user is the creator
        #         current_user_id = user["user_id"]
        #
        #         if not context.is_private or context.created_by == current_user_id:
        #         else:
        #             # Private context, non-creator: mask the name

        # Get credentials status (API Keys + OAuth Apps)
        # API Keys
        api_keys_result = await db.execute(
            select(
                func.count(APIKey.id).label("api_key_count"),
                func.bool_or(APIKey.hidden_at.is_(None)).label("any_visible"),
            ).where(
                and_(
                    APIKey.user_id == m.user_id,
                    APIKey.workspace_id == workspace_id,
                    APIKey.revoked_at.is_(None),
                )
            )
        )
        api_key_stats = api_keys_result.one_or_none()
        api_key_count = (
            api_key_stats.api_key_count if api_key_stats and api_key_stats.api_key_count else 0
        )
        api_key_visible = (
            bool(api_key_stats.any_visible)
            if api_key_stats and api_key_stats.any_visible is not None
            else False
        )

        # OAuth Apps by provider
        oauth_apps_result = await db.execute(
            select(OAuth2Client).where(
                and_(
                    OAuth2Client.owner_id == m.user_id,
                    OAuth2Client.workspace_id == workspace_id,
                )
            )
        )
        oauth_apps = oauth_apps_result.scalars().all()

        claude_app = next((a for a in oauth_apps if a.provider == "claude"), None)
        chatgpt_app = next((a for a in oauth_apps if a.provider == "chatgpt"), None)
        custom_apps = [a for a in oauth_apps if a.provider == "custom"]

        credentials_status = CredentialsStatusInfo(
            api_key_count=api_key_count,
            api_key_visible=api_key_visible,
            claude_app_visible=claude_app.hidden_at is None if claude_app else None,
            chatgpt_app_visible=chatgpt_app.hidden_at is None if chatgpt_app else None,
            custom_app_count=len(custom_apps),
        )

        # Issue #234: Convert allowed_context_ids to string list
        allowed_context_ids_str = None
        if m.allowed_context_ids is not None:
            allowed_context_ids_str = [str(ctx_id) for ctx_id in m.allowed_context_ids]

        member_responses.append(
            WorkspaceMemberResponse(
                user_id=m.user_id,
                user_name=user_record.name if user_record else None,
                user_email=user_record.email if user_record else None,
                role=m.role,
                joined_at=to_utc_iso(m.joined_at),
                credentials_status=credentials_status,
                last_login_at=to_utc_iso(user_record.last_login_at) if user_record else None,
                allowed_context_ids=allowed_context_ids_str,
            )
        )

    return member_responses


@router.post("/{workspace_id}/members", response_model=WorkspaceMemberResponse, status_code=201)
async def add_member(
    workspace_id: UUID,
    body: AddMemberRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Add a member to workspace.

    Requires admin or owner role.
    """
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # Check admin access
    await perm_service.check_workspace_admin(user["user_id"], workspace_id)

    member = await workspace_service.add_member(
        workspace_id=workspace_id,
        user_id=body.user_id,
        role=body.role,
        invited_by=user["user_id"],
    )

    return WorkspaceMemberResponse(
        user_id=member.user_id,
        role=member.role,
        joined_at=to_utc_iso(member.joined_at),
    )


@router.put("/{workspace_id}/members/{user_id}", response_model=WorkspaceMemberResponse)
async def update_member_role(
    workspace_id: UUID,
    user_id: str,
    body: UpdateMemberRoleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update member's role.

    Requires admin or owner role.
    """
    current_user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # Check admin access
    current_member = await perm_service.check_workspace_admin(current_user["user_id"], workspace_id)

    # Issue #254: Prevent users from changing their own role
    if user_id == current_user["user_id"]:
        raise HTTPException(
            status_code=403,
            detail="Cannot modify your own role. Another administrator must change your role.",
        )

    # Issue #254: Prevent non-owners from changing owner's role
    target_member = await workspace_service.get_member(workspace_id, user_id)
    if target_member.role == WorkspaceRole.OWNER and current_member.role != WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=403,
            detail="Only the owner can change the owner's role.",
        )

    member = await workspace_service.update_member_role(
        workspace_id=workspace_id,
        user_id=user_id,
        new_role=body.role,
    )

    return WorkspaceMemberResponse(
        user_id=member.user_id,
        role=member.role,
        joined_at=to_utc_iso(member.joined_at),
    )


@router.put("/{workspace_id}/members/{user_id}/context-access")
async def update_member_context_access(
    workspace_id: UUID,
    user_id: str,
    body: UpdateMemberContextAccessRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update member's allowed context access.

    Issue #234: Context access restriction for member/viewer.

    - allowed_context_ids=null: No restriction (can access all contexts)
    - allowed_context_ids=[]: No context access
    - allowed_context_ids=[uuid1, uuid2]: Only these contexts accessible

    Note: This setting only applies to member/viewer roles.
    owner/admin roles always have full access (this setting is ignored).

    Requires admin or owner role.
    """
    current_user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # Check admin access
    await perm_service.check_workspace_admin(current_user["user_id"], workspace_id)

    # Convert string UUIDs to UUID objects
    allowed_context_ids = None
    if body.allowed_context_ids is not None:
        # Validate that all context IDs exist in this workspace
        if body.allowed_context_ids:
            context_result = await db.execute(
                select(Context.id).where(
                    Context.workspace_id == workspace_id,
                    Context.id.in_([UUID(ctx_id) for ctx_id in body.allowed_context_ids]),
                    Context.deleted_at.is_(None),
                )
            )
            valid_ids = {row[0] for row in context_result.all()}
            invalid_ids = set(body.allowed_context_ids) - {str(id) for id in valid_ids}

            if invalid_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid context IDs: {list(invalid_ids)}",
                )

            allowed_context_ids = [UUID(ctx_id) for ctx_id in body.allowed_context_ids]
        else:
            allowed_context_ids = []  # Empty list = no access

    member = await workspace_service.update_member_context_access(
        workspace_id=workspace_id,
        user_id=user_id,
        allowed_context_ids=allowed_context_ids,
    )

    # Convert back to string list for response
    allowed_context_ids_str = None
    if member.allowed_context_ids is not None:
        allowed_context_ids_str = [str(ctx_id) for ctx_id in member.allowed_context_ids]

    return {
        "status": "ok",
        "user_id": member.user_id,
        "allowed_context_ids": allowed_context_ids_str,
    }


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(
    workspace_id: UUID,
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Remove member from workspace.

    Issue #217: Requires owner role only. Cannot remove owner.
    """
    current_user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # Issue #217: Only owner can remove members
    await perm_service.check_workspace_owner(current_user["user_id"], workspace_id)

    await workspace_service.remove_member(workspace_id, user_id)


# ============================================================================
# Workspace Statistics
# ============================================================================


@router.get("/{workspace_id}/stats")
async def get_workspace_stats(
    workspace_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Get workspace statistics.

    Returns:
        - total_memories: Total memories across all contexts
        - context_count: Number of contexts
        - member_count: Number of members
    """
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check
    # Issue #269: Membership verification is sufficient (no current_workspace_id required)
    # Users can access stats for any workspace they are a member of
    await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.MEMBER
    )

    stats = await workspace_service.get_workspace_stats(workspace_id)

    return stats


@router.get("/{workspace_id}/contexts/stats", response_model=WorkspaceContextStatsResponse)
async def get_context_stats(
    workspace_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceContextStatsResponse:
    """Get per-context usage statistics.

    Issue #249: Context usage overview for workspace page.

    Returns context-level statistics including memory count, storage,
    last activity, and member count for each context.

    Security:
        - Requires workspace Owner or Admin role
        - Only shows contexts user has access to (RBAC)

    Returns:
        WorkspaceContextStatsResponse with per-context stats and totals
    """
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check
    # Issue #269: Membership verification is sufficient (no current_workspace_id required)
    await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.MEMBER
    )

    stats = await workspace_service.get_context_stats(workspace_id)

    return WorkspaceContextStatsResponse(**stats)


@router.get(
    "/{workspace_id}/contexts/{context_id}/usage-timeline",
    response_model=ContextUsageTimelineResponse,
)
async def get_context_usage_timeline(
    workspace_id: UUID,
    context_id: UUID,
    request: Request,
    days: int = 7,
    db: AsyncSession = Depends(get_db),
) -> ContextUsageTimelineResponse:
    """Get time-series usage statistics for a specific context.

    Issue #249: Context activity timeline for graphs.

    Args:
        workspace_id: Workspace ID
        context_id: Context ID
        days: Number of days to include (default: 7, max: 30)

    Returns:
        Daily API call counts and unique users
    """
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check
    # Issue #269: Membership verification is sufficient (no current_workspace_id required)
    await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.MEMBER
    )

    # Limit days to prevent excessive queries
    days = min(days, 30)

    timeline = await workspace_service.get_context_usage_timeline(workspace_id, context_id, days)

    return ContextUsageTimelineResponse(**timeline)


@router.get(
    "/{workspace_id}/contexts/{context_id}/user-activity",
    response_model=ContextUserActivityResponse,
)
async def get_context_user_activity(
    workspace_id: UUID,
    context_id: UUID,
    request: Request,
    days: int = 7,
    db: AsyncSession = Depends(get_db),
) -> ContextUserActivityResponse:
    """Get per-user activity statistics for a specific context.

    Issue #249: User activity breakdown for context.

    Args:
        workspace_id: Workspace ID
        context_id: Context ID
        days: Number of days to include (default: 7, max: 30)

    Returns:
        Per-user API call counts and last activity
    """
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check
    # Issue #269: Membership verification is sufficient (no current_workspace_id required)
    await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.ADMIN
    )

    # Limit days to prevent excessive queries
    days = min(days, 30)

    activity = await workspace_service.get_context_user_activity(workspace_id, context_id, days)

    return ContextUserActivityResponse(**activity)


@router.get("/{workspace_id}/memory-timeline", response_model=MemoryTimelineResponse)
async def get_workspace_memory_timeline(
    workspace_id: UUID,
    request: Request,
    days: int = 30,
    context_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> MemoryTimelineResponse:
    """Get workspace memory creation timeline.

    Issue #275 Task 6: Memory count timeline visualization.
    Issue #134: Optional context_id filter for dashboard global filter.
    """
    user = await get_current_user(request)
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.MEMBER
    )

    days = min(days, 90)  # Performance limit

    timeline = await workspace_service.get_workspace_memory_timeline(
        workspace_id, days, context_id=context_id
    )

    return MemoryTimelineResponse(**timeline)


@router.get(
    "/{workspace_id}/contexts/{context_id}/public-api-stats", response_model=PublicAPIStatsResponse
)
async def get_context_public_api_stats(
    workspace_id: UUID,
    context_id: UUID,
    request: Request,
    days: int = 7,
    db: AsyncSession = Depends(get_db),
) -> PublicAPIStatsResponse:
    """Get Public API usage statistics for a public context.

    Issue #265: Resource Ingest API and Public Search API stats.

    Args:
        workspace_id: Workspace ID
        context_id: Context ID
        days: Number of days (default: 7, max: 30)

    Returns:
        PublicAPIStatsResponse with resource_ingest and public_search stats

    Security:
        - Requires Workspace Member or higher
        - Context must be public (is_public=true)

    Example:
        GET /api/v1/workspaces/{workspace_id}/contexts/{context_id}/public-api-stats?days=7
    """
    # Get user from session
    user = await get_current_user(request)

    # Initialize services
    workspace_service = WorkspaceService(db)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check
    # Issue #269: Membership verification is sufficient (no current_workspace_id required)
    await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.MEMBER
    )

    # Limit days to prevent excessive queries
    days = min(days, 30)

    # Get public API stats (will raise ValidationError if context is not public)
    stats = await workspace_service.get_context_public_api_stats(workspace_id, context_id, days)

    return PublicAPIStatsResponse(**stats)


# ============================================================================
# OpenAI API Key Status (Issue #181)
# ============================================================================


@router.get(
    "/{workspace_id}/openai-key-status",
    response_model=OpenAIKeyStatusResponse,
    # Issue #1167: this probe is a BYOK read surface — 404 when ENABLE_BYOK
    # is false. Leaving it up would report has_key=false in deployments where
    # env keys serve embeddings, and the contexts page blocks creation on
    # has_key=false. Both frontend consumers degrade gracefully on failure.
    dependencies=[Depends(require_byok_enabled)],
)
async def check_openai_key_status(
    workspace_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> OpenAIKeyStatusResponse:
    """Check if workspace has valid OpenAI API key.

    Issue #181: OpenAI API key guidance in context creation.

    Returns status about whether workspace has an enabled OpenAI API key,
    and whether current user has permissions to configure it.

    Args:
        workspace_id: Workspace ID
        request: HTTP request
        db: Database session

    Returns:
        OpenAIKeyStatusResponse with has_key, can_configure, external_keys_url

    Raises:
        HTTPException 403: User not member of workspace
    """
    user = await get_current_user(request)
    perm_service = PermissionService(db)

    # Check access (any member can view) and get member info
    workspace_member = await perm_service.check_workspace_access(
        user["user_id"], workspace_id, required_role=WorkspaceRole.VIEWER
    )

    # Query for OpenAI key (prioritize workspace-scoped, fallback to user-scoped)
    # First try workspace-scoped
    stmt = select(ExternalAPIKey).where(
        ExternalAPIKey.workspace_id == workspace_id,
        ExternalAPIKey.key_name == "OPENAI_API_KEY",
        ExternalAPIKey.enabled.is_(True),
    )
    result = await db.execute(stmt)
    openai_key = result.scalar_one_or_none()

    # Fallback to user-scoped (legacy) if not found
    if not openai_key:
        stmt = select(ExternalAPIKey).where(
            ExternalAPIKey.workspace_id.is_(None),
            ExternalAPIKey.user_id == user["user_id"],  # Filter by user for user-scoped keys
            ExternalAPIKey.key_name == "OPENAI_API_KEY",
            ExternalAPIKey.enabled.is_(True),
        )
        result = await db.execute(stmt)
        openai_key = result.scalar_one_or_none()

    # Check if user can configure (owner/admin)
    can_configure = workspace_member.role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN)

    logger.info(
        f"User {user['user_id']} checked OpenAI key status for workspace {workspace_id}: "
        f"has_key={openai_key is not None}, can_configure={can_configure}"
    )

    return OpenAIKeyStatusResponse(
        has_key=openai_key is not None,
        can_configure=can_configure,
        external_keys_url="/integrations/external-keys",
    )


# ============================================================================
# Ownership transfer (Issue #1094)
# ============================================================================


class TransferOwnershipRequest(BaseModel):
    """Request to transfer workspace ownership to an existing member."""

    target_user_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="User ID of an existing workspace member to promote to owner.",
    )


class TransferOwnershipResponse(BaseModel):
    """Result of an ownership transfer (or idempotent no-op)."""

    workspace_id: str
    previous_owner_id: str
    new_owner_id: str
    ownership_epoch: int
    changed: bool = Field(
        ..., description="False when the target already owned the workspace (no-op)."
    )


@router.post("/{workspace_id}/transfer-ownership", response_model=TransferOwnershipResponse)
async def transfer_workspace_ownership(
    workspace_id: UUID,
    body: TransferOwnershipRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> TransferOwnershipResponse:
    """Transfer ownership of a workspace to an existing member.

    Current-owner-only and session-only (a Bearer API key / OAuth token is
    rejected 403 — a sensitive governance action must require a real browser
    session). The owner check binds to the **path** ``workspace_id``, never the
    caller's current workspace. Atomic single-owner invariant under a row lock,
    idempotent, and writes an audit row + bumps the ownership epoch on success.
    """
    user_id = get_user_id(user)
    # Owner-only against the explicit path workspace_id (#389 cross-tenant guard).
    await PermissionService(db).check_workspace_owner(user_id, workspace_id)

    result = await WorkspaceOwnershipService(db).transfer_ownership(
        workspace_id=workspace_id,
        current_owner_id=user_id,
        target_user_id=body.target_user_id,
        performed_by_email=str(user.get("email") or user_id),
    )

    # Courtesy-notify the new owner (#1103) — only on an actual change (skip the
    # idempotent no-op), best-effort: the transfer already committed, so a
    # notification failure must never surface to the caller.
    if result.changed:
        await _notify_new_owner_best_effort(db, workspace_id, result.new_owner_id)

    return TransferOwnershipResponse(
        workspace_id=str(result.workspace_id),
        previous_owner_id=result.previous_owner_id,
        new_owner_id=result.new_owner_id,
        ownership_epoch=result.ownership_epoch,
        changed=result.changed,
    )


async def _notify_new_owner_best_effort(
    db: AsyncSession, workspace_id: UUID, new_owner_id: str
) -> None:
    """Email the new owner that the workspace was transferred to them (#1103).

    Best-effort: the transfer is already committed, so the WHOLE block is guarded
    — neither the email resolution queries nor the (no-raise) EmailService call
    may surface to the caller. A new owner without an email is simply skipped.
    """
    try:
        email = (
            await db.execute(select(User.email).where(User.user_id == new_owner_id))
        ).scalar_one_or_none()
        if not email:
            return
        name = (
            await db.execute(select(Workspace.name).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        await get_email_service().send_workspace_ownership_transferred(
            to_email=email,
            workspace_name=name or "your workspace",
        )
    except Exception as exc:
        # Notification is best-effort — swallow everything (resolution query or
        # provider error) so it never affects the committed transfer. Log the
        # exception TYPE only, not str(exc): a SQLAlchemy error string echoes the
        # bound parameters (here the client-supplied new_owner_id), so we keep the
        # same no-request-field-echo discipline as the Resend provider layer.
        logger.warning(
            "ownership_transfer_notify_failed",
            workspace_id=str(workspace_id),
            error_type=type(exc).__name__,
        )
