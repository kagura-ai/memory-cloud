"""Admin API Routes - User Management and System-wide Statistics.

Admin-only endpoints for managing users and viewing system-wide statistics.
Requires Admin role for all endpoints.
Issue #106: Refactored to use consolidated utilities
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_admin
from db.base import get_db
from models.auth import (
    APIKey,
    Context,
    ContextMember,
    OAuth2Client,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from utils import db_transaction, get_user_id
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ============================================================================
# Schemas
# ============================================================================


class UserInfo(BaseModel):
    """User information for admin view.

    Issue #164: Extended with workspaces field.
    Issue #175: Added timezone field for user preferences.
    """

    model_config = {
        "json_encoders": {
            datetime: lambda v: (
                v.isoformat() + "Z" if v and v.tzinfo is None else v.isoformat() if v else None
            )
        }
    }

    id: str
    email: str
    name: str
    picture: str | None = None
    role: str
    created_at: datetime
    last_login: datetime | None = None
    memory_count: int
    is_active: bool
    timezone: str = "UTC"  # Issue #175: User timezone preference
    auth_provider: str | None = None  # Issue #361: Registration provider
    workspaces: list[dict] = []  # Issue #164: Workspace memberships

    # Issue #246: current_context_id removed
    # current_context_id: str | None = None
    # current_context_name: str | None = None
    # current_context_display_name: str | None = None


class UserStats(BaseModel):
    """Detailed statistics for a specific user."""

    user: dict
    memories: dict
    api_usage: dict


class UserListResponse(BaseModel):
    """Response for user list."""

    users: list[UserInfo]
    total: int


class RoleUpdateRequest(BaseModel):
    """Request to update user role."""

    role: str


# ============================================================================
# Admin Endpoints
# ============================================================================


@router.get("/users", response_model=UserListResponse)
async def list_users(
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_workspaces: bool = Query(False),  # Issue #164: Include workspace info
    search: str | None = Query(None),  # Issue #164: Search by email/name
    workspace_id: str | None = Query(None),  # Issue #164: Filter by workspace
    role: str | None = Query(None),  # Issue #164: Filter by system role
    plan: str | None = Query(None),  # Issue #164: Filter by plan tier
    sort: str = Query("created_at"),  # Issue #164: Sort field
):
    """List all users with their memory statistics.

    Admin-only endpoint.

    Issue #164: Extended with search/filter capabilities and workspace info.

    Args:
        user: Authenticated admin user
        db: Database session
        limit: Maximum number of users to return
        offset: Pagination offset
        include_workspaces: Include workspace membership info
        search: Search by email or name (partial match)
        workspace_id: Filter by workspace ID
        role: Filter by system role (admin/user)
        plan: Filter by plan tier (free/basic/pro)
        sort: Sort field (created_at, last_login, memory_count)

    Returns:
        List of users with memory counts, storage usage, and optionally workspace info
    """
    try:
        from uuid import UUID

        # Build base query
        stmt = select(User)

        # Issue #164: Apply search filter
        if search:
            search_pattern = f"%{search}%"
            stmt = stmt.where(
                or_(User.email.ilike(search_pattern), User.name.ilike(search_pattern))
            )

        # Issue #164: Apply role filter
        if role:
            stmt = stmt.where(User.role == role)

        # Issue #164: Apply workspace filter
        if workspace_id:
            stmt = stmt.join(WorkspaceMember).where(
                WorkspaceMember.workspace_id == UUID(workspace_id)
            )

        # Issue #164: Apply plan filter (via workspace)
        if plan:
            if not workspace_id:  # If not already joined
                stmt = stmt.join(WorkspaceMember)
            stmt = stmt.join(Workspace).where(Workspace.plan_name == plan)

        # Get total count (with filters applied)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0

        # Issue #164: Apply sorting
        if sort == "last_login":
            stmt = stmt.order_by(User.last_login_at.desc().nullslast())
        elif sort == "memory_count":
            # TODO: Subquery for memory count sorting (complex, skip for now)
            stmt = stmt.order_by(User.created_at.desc())
        else:  # default: created_at
            stmt = stmt.order_by(User.created_at.desc())

        # Execute query with pagination
        result = await db.execute(stmt.limit(limit).offset(offset))
        users_list = list(result.scalars().all())

        # Issue #164: Fix N+1 query - Bulk aggregation for memory stats
        user_ids = [u.user_id for u in users_list]
        memory_stats_dict = {}

        if user_ids:
            # Single query for all memory stats
            memory_stats_result = await db.execute(
                select(
                    Memory.user_id,
                    func.count(Memory.id).label("memory_count"),
                )
                .where(Memory.user_id.in_(user_ids), Memory.deleted_at.is_(None))
                .group_by(Memory.user_id)
            )

            for row in memory_stats_result.all():
                memory_stats_dict[row.user_id] = {
                    "memory_count": row.memory_count or 0,
                }

        # Issue #246: current_context_id removed - skip context lookup
        # from models.auth import Context
        # context_map = {}
        # user_ids_with_context = [u.user_id for u in users_list if u.current_context_id]

        # Build user info list
        user_infos = []
        for u in users_list:
            stats = memory_stats_dict.get(u.user_id, {"memory_count": 0})
            # Issue #246: current_context_id removed
            # context_info = context_map.get(u.user_id, {})

            user_infos.append(
                UserInfo(
                    id=u.user_id,
                    email=u.email,
                    name=u.name,
                    picture=u.picture,
                    role=u.role or "user",
                    created_at=u.created_at,
                    last_login=u.last_login_at,
                    memory_count=stats["memory_count"],
                    is_active=(u.last_login_at is not None),
                    timezone=u.timezone or "UTC",  # Issue #175: User timezone preference
                    auth_provider=u.auth_provider,  # Issue #361
                    workspaces=[],  # Will be populated below if include_workspaces=True
                    # Issue #246: current_context_id removed
                    # current_context_id=context_info.get('context_id'),
                    # current_context_name=context_info.get('context_name'),
                    # current_context_display_name=context_info.get('context_display_name'),
                )
            )

        # Issue #164: Load workspace memberships if requested
        if include_workspaces and users_list:
            user_ids = [u.user_id for u in users_list]
            workspace_memberships_result = await db.execute(
                select(WorkspaceMember, Workspace)
                .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                .where(WorkspaceMember.user_id.in_(user_ids))
                .order_by(WorkspaceMember.role.desc())
            )
            workspace_memberships = workspace_memberships_result.all()

            # Build dict: {user_id: [workspace_info, ...]}
            user_workspaces_map = {}
            for member, workspace in workspace_memberships:
                if member.user_id not in user_workspaces_map:
                    user_workspaces_map[member.user_id] = []

                # Find user to check if primary workspace
                user_obj = next((u for u in users_list if u.user_id == member.user_id), None)
                is_primary = user_obj and user_obj.current_workspace_id == workspace.id

                user_workspaces_map[member.user_id].append(
                    {
                        "workspace_id": str(workspace.id),
                        "workspace_name": workspace.name,
                        "role": member.role,
                        "is_primary": is_primary,
                        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                    }
                )

            # Attach workspace info to user_infos
            for user_info in user_infos:
                user_info.workspaces = user_workspaces_map.get(user_info.id, [])

        logger.info(
            "admin_list_users",
            count=len(user_infos),
            total=total,
            include_workspaces=include_workspaces,
        )

        return UserListResponse(users=user_infos, total=total)

    except Exception as e:
        logger.error("admin_list_users_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list users",
        ) from e


@router.get("/users/{user_id}/stats", response_model=UserStats)
async def get_user_stats(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed statistics for a specific user.

    Admin-only endpoint.

    Args:
        user_id: Target user ID
        admin: Authenticated admin user
        db: Database session

    Returns:
        Detailed user statistics including memory breakdown
    """
    try:
        # Get user info (user_id is OAuth2 user_id string, not integer id)
        result = await db.execute(select(User).where(User.user_id == user_id))
        target_user = result.scalar_one_or_none()

        if not target_user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        # Get memory stats
        total_count_result = await db.execute(
            select(func.count(Memory.id)).where(Memory.user_id == user_id)
        )
        total_count = total_count_result.scalar() or 0

        # Count by scope (working vs persistent)
        working_count_result = await db.execute(
            select(func.count(Memory.id)).where(
                and_(Memory.user_id == user_id, Memory.scope == "working")
            )
        )
        working_count = working_count_result.scalar() or 0

        persistent_count = total_count - working_count

        # Count by type
        type_result = await db.execute(
            select(Memory.type, func.count(Memory.id))
            .where(Memory.user_id == user_id)
            .group_by(Memory.type)
        )
        by_type = {row[0]: row[1] for row in type_result.all()}

        # API usage (from API keys)
        api_key_count_result = await db.execute(
            select(func.count(APIKey.id)).where(
                and_(APIKey.user_id == user_id, APIKey.revoked_at.is_(None))
            )
        )
        api_key_count = api_key_count_result.scalar() or 0

        logger.info("admin_get_user_stats", user_id=user_id, total=total_count)

        return UserStats(
            user={
                "id": target_user.id,
                "email": target_user.email,
                "name": target_user.name,
                "role": target_user.role or "user",
            },
            memories={
                "total": total_count,
                "working": working_count,
                "persistent": persistent_count,
                "by_type": by_type,
            },
            api_usage={"active_api_keys": api_key_count, "mcp_connections": 0},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("admin_get_user_stats_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user statistics",
        ) from e


@router.get("/users/{user_id}")
async def get_user_detail(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get comprehensive user details including workspaces, contexts, and stats.

    Issue #164: User detail page.

    Args:
        user_id: Target user ID (OAuth2 user_id)
        admin: Authenticated admin user
        db: Database session

    Returns:
        UserDetailResponse with user info, workspaces, accessible contexts, and stats
    """
    from models.schemas import UserAccessibleContext, UserDetailResponse, UserWorkspaceInfo

    try:
        # 1. Get user
        user_result = await db.execute(select(User).where(User.user_id == user_id))
        target_user = user_result.scalar_one_or_none()

        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        # 2. Get workspaces
        workspace_memberships_result = await db.execute(
            select(WorkspaceMember, Workspace)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .where(WorkspaceMember.user_id == user_id)
            .order_by(WorkspaceMember.role.desc())
        )
        workspace_memberships = workspace_memberships_result.all()

        workspaces = [
            UserWorkspaceInfo(
                workspace_id=str(workspace.id),
                workspace_name=workspace.name,
                role=member.role,
                is_primary=(target_user.current_workspace_id == workspace.id),
                joined_at=member.joined_at,
                plan_name=workspace.plan_name,  # Issue #164: Include current plan
            )
            for member, workspace in workspace_memberships
        ]

        # 3. Get accessible contexts
        from services.permission_service import PermissionService

        perm_service = PermissionService(db)
        accessible_contexts = []

        # Critical Fix: Avoid O(N²) queries - collect all contexts first, then batch fetch ContextMembers
        all_contexts = []
        context_workspace_map = {}
        workspace_role_map = {}

        for member, workspace in workspace_memberships:
            try:
                contexts = await perm_service.get_accessible_contexts(user_id, workspace.id)
                for ctx in contexts:
                    all_contexts.append(ctx)
                    context_workspace_map[ctx.id] = (workspace, member.role)
                    workspace_role_map[ctx.id] = member.role
            except HTTPException:
                # Skip if no access
                pass

        # Batch fetch all ContextMembers for this user
        if all_contexts:
            context_ids = [ctx.id for ctx in all_contexts]
            ctx_members_result = await db.execute(
                select(ContextMember).where(
                    ContextMember.context_id.in_(context_ids), ContextMember.user_id == user_id
                )
            )
            ctx_members_by_context = {cm.context_id: cm for cm in ctx_members_result.scalars()}
        else:
            ctx_members_by_context = {}

        # Build response using cached data
        for ctx in all_contexts:
            workspace, workspace_role = context_workspace_map[ctx.id]

            # Determine user's role in this context
            ctx_role = "viewer"  # Default
            if workspace_role in ("owner", "admin"):
                ctx_role = "owner"  # Workspace owner/admin have full access
            else:
                # Check context_members for explicit role
                ctx_member = ctx_members_by_context.get(ctx.id)
                if ctx_member:
                    ctx_role = ctx_member.role

            accessible_contexts.append(
                UserAccessibleContext(
                    context_id=str(ctx.id),
                    context_name=ctx.name,
                    workspace_id=str(workspace.id),
                    workspace_name=workspace.name,
                    role=ctx_role,
                    last_used_at=ctx.last_used_at,
                )
            )

        # 4. Get stats (reuse logic from get_user_stats)
        total_memories_result = await db.execute(
            select(func.count(Memory.id)).where(Memory.user_id == user_id)
        )
        total_memories = total_memories_result.scalar() or 0

        working_memories_result = await db.execute(
            select(func.count(Memory.id)).where(
                and_(Memory.user_id == user_id, Memory.scope == "working")
            )
        )
        working_memories = working_memories_result.scalar() or 0

        persistent_memories = total_memories - working_memories

        # Count active API keys
        api_keys_result = await db.execute(
            select(func.count(APIKey.id)).where(
                and_(APIKey.user_id == user_id, APIKey.revoked_at.is_(None))
            )
        )
        active_api_keys = api_keys_result.scalar() or 0

        stats = {
            "total_memories": total_memories,
            "working_memories": working_memories,
            "persistent_memories": persistent_memories,
            "active_api_keys": active_api_keys,
        }

        # 5. Build response
        from models.schemas import UserDetailResponse

        return UserDetailResponse(
            user={
                "id": target_user.user_id,
                "user_id": target_user.user_id,  # Add for backward compatibility
                "email": target_user.email,
                "name": target_user.name,
                "picture": target_user.picture,
                "role": target_user.role,
                "is_initial_admin": getattr(target_user, "is_initial_admin", False),
                "created_at": target_user.created_at.isoformat() + "Z"
                if target_user.created_at.tzinfo is None
                else target_user.created_at.isoformat(),
                "last_login_at": (
                    target_user.last_login_at.isoformat() + "Z"
                    if target_user.last_login_at.tzinfo is None
                    else target_user.last_login_at.isoformat()
                )
                if target_user.last_login_at
                else None,
                "auth_provider": target_user.auth_provider,
            },
            workspaces=workspaces,
            accessible_contexts=accessible_contexts,
            stats=stats,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("admin_get_user_detail_failed", user_id=user_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get user details") from e


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    request: RoleUpdateRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update user role (Admin-only)."""
    admin_id = get_user_id(admin)

    # Validate role
    valid_roles = ["admin", "user", "read_only"]
    if request.role not in valid_roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}",
        )

    # Prevent self-demotion
    if admin_id == user_id and request.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot change your own admin role",
        )

    async with db_transaction(db, "update_user_role", "Failed to update user role"):
        result = await db.execute(select(User).where(User.user_id == user_id))
        target_user = result.scalar_one_or_none()

        if not target_user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        old_role = target_user.role
        target_user.role = request.role
        await db.commit()

        logger.info(
            "admin_update_user_role",
            user_id=user_id,
            old_role=old_role,
            new_role=request.role,
        )

        return {
            "message": f"User role updated to {request.role}",
            "user_id": user_id,
            "new_role": request.role,
        }


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a user and all their data (Admin-only).

    Protection:
    - Cannot delete initial admin (is_initial_admin=True)
    - Cannot delete last remaining admin
    - Cannot delete your own account

    Issue #166: System Admin protection

    Deletes memories, API keys, OAuth2 clients, Qdrant collection, and user account.
    """
    from services.system_admin_service import SystemAdminService

    admin_id = get_user_id(admin)

    # Prevent self-deletion
    if admin_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete your own account",
        )

    # Issue #166: Check if user is a protected system admin
    service = SystemAdminService(db)
    can_delete, reason = await service.can_delete_admin(user_id)
    if not can_delete:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)

    async with db_transaction(db, "delete_user", "Failed to delete user"):
        result = await db.execute(select(User).where(User.user_id == user_id))
        target_user = result.scalar_one_or_none()

        if not target_user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        # Count data to be deleted
        memory_count_result = await db.execute(
            select(func.count(Memory.id)).where(Memory.user_id == user_id)
        )
        memory_count = memory_count_result.scalar() or 0

        api_key_count_result = await db.execute(
            select(func.count(APIKey.id)).where(APIKey.user_id == user_id)
        )
        api_key_count = api_key_count_result.scalar() or 0

        # Delete memories and API keys
        await db.execute(Memory.__table__.delete().where(Memory.user_id == user_id))
        await db.execute(APIKey.__table__.delete().where(APIKey.user_id == user_id))

        # Delete OAuth2 clients owned by the user
        await db.execute(OAuth2Client.__table__.delete().where(OAuth2Client.owner_id == user_id))

        # Delete contexts owned by the user
        await db.execute(Context.__table__.delete().where(Context.created_by == user_id))

        # Remove user from workspace memberships
        await db.execute(
            WorkspaceMember.__table__.delete().where(WorkspaceMember.user_id == user_id)
        )

        # Delete workspaces owned by the user
        await db.execute(Workspace.__table__.delete().where(Workspace.owner_user_id == user_id))

        # Note: Qdrant points are automatically cleaned up via context deletion cascade
        # No need for legacy collection-per-user deletion

        # Delete user
        await db.delete(target_user)
        await db.commit()

        logger.info(
            "admin_delete_user",
            user_id=user_id,
            email=target_user.email,
            memories_deleted=memory_count,
            api_keys_deleted=api_key_count,
        )

        return {
            "message": "User deleted successfully",
            "user_id": user_id,
            "deleted": {
                "memories": memory_count,
                "api_keys": api_key_count,
            },
        }


# ============================================================================
# Embedding Retry (Issue #93)
# ============================================================================


@router.post("/embedding/retry-failed")
async def retry_failed_embeddings(
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    context_id: str | None = Query(None, description="Filter by context ID (UUID)"),
    workspace_id: str | None = Query(None, description="Filter by workspace ID (UUID)"),
) -> dict:
    """Reset failed embeddings to pending for automatic retry.

    Issue #93: Admin tool to recover from embedding failures.
    """
    from uuid import UUID as PyUUID

    from sqlalchemy import update

    # Require at least one filter to prevent accidental mass retry
    if not context_id and not workspace_id:
        raise HTTPException(
            status_code=400,
            detail="At least one of context_id or workspace_id is required",
        )

    # Validate UUID formats
    for param_name, param_val in [("context_id", context_id), ("workspace_id", workspace_id)]:
        if param_val:
            try:
                PyUUID(param_val)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid {param_name} format") from e

    conditions = [
        Memory.embedding_status == "failed",
        Memory.deleted_at.is_(None),
    ]
    if context_id:
        conditions.append(Memory.context_id == context_id)
    if workspace_id:
        conditions.append(Memory.workspace_id == workspace_id)

    stmt = (
        update(Memory).where(*conditions).values(embedding_status="pending", embedding_error=None)
    )
    result = await db.execute(stmt)
    await db.commit()

    reset_count = result.rowcount
    logger.info(
        "admin_retry_failed_embeddings",
        reset_count=reset_count,
        context_id=context_id,
        workspace_id=workspace_id,
        admin_user_id=get_user_id(user),
    )

    return {"status": "success", "reset_count": reset_count}


# ============================================================================
# Context Recovery from Qdrant (Issue #86)
# ============================================================================


class ContextRecoveryRequest(BaseModel):
    """Request to recover a deleted context from Qdrant data."""

    context_id: str
    workspace_id: str | None = None
    context_name: str | None = None
    dry_run: bool = True


class ContextRecoveryResponse(BaseModel):
    """Result of context recovery attempt."""

    context_id: str
    workspace_id: str
    qdrant_points_found: int
    memories_recovered: int
    memories_already_existed: int
    context_record_created: bool
    search_config_restored: bool
    dry_run: bool
    errors: list[str]


@router.post("/contexts/recover", response_model=ContextRecoveryResponse)
async def recover_context(
    request_body: ContextRecoveryRequest,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ContextRecoveryResponse:
    """Recover a deleted context from surviving Qdrant data.

    Issue #86: Admin endpoint to reconstruct context and memory records
    from Qdrant point payloads when a context has been accidentally deleted.

    Default is dry_run=True — shows what would be recovered without making changes.
    """
    from uuid import UUID as PyUUID

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    from config.settings import get_settings
    from db.qdrant import get_qdrant_client
    from models.config import ContextSearchConfig

    settings = get_settings()
    client = get_qdrant_client()
    collection_name = settings.qdrant_collection_name
    context_id = request_body.context_id
    errors: list[str] = []

    # Step 1: Scroll Qdrant for all points with this context_id
    all_points = []
    offset = None
    while True:
        points, next_offset = await client.scroll(
            collection_name=collection_name,
            scroll_filter=Filter(
                must=[FieldCondition(key="context_id", match=MatchValue(value=context_id))]
            ),
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        all_points.extend(points)
        if next_offset is None:
            break
        offset = next_offset

    if not all_points:
        return ContextRecoveryResponse(
            context_id=context_id,
            workspace_id=request_body.workspace_id or "",
            qdrant_points_found=0,
            memories_recovered=0,
            memories_already_existed=0,
            context_record_created=False,
            search_config_restored=False,
            dry_run=request_body.dry_run,
            errors=["No Qdrant points found for this context_id"],
        )

    # Step 2: Determine workspace_id from first point if not provided
    first_payload = all_points[0].payload or {}
    workspace_id = request_body.workspace_id or first_payload.get("workspace_id", "")
    if not workspace_id:
        return ContextRecoveryResponse(
            context_id=context_id,
            workspace_id="",
            qdrant_points_found=len(all_points),
            memories_recovered=0,
            memories_already_existed=0,
            context_record_created=False,
            search_config_restored=False,
            dry_run=request_body.dry_run,
            errors=["Cannot determine workspace_id from Qdrant data. Please provide it."],
        )

    if request_body.dry_run:
        # Just report what we found
        # Check if context record exists
        existing_context = await db.execute(select(Context).where(Context.id == PyUUID(context_id)))
        context_exists = existing_context.scalar_one_or_none() is not None

        # Check how many memory records already exist
        existing_mem_ids = set()
        for point in all_points:
            existing = await db.execute(select(Memory.id).where(Memory.id == PyUUID(str(point.id))))
            if existing.scalar_one_or_none() is not None:
                existing_mem_ids.add(str(point.id))

        return ContextRecoveryResponse(
            context_id=context_id,
            workspace_id=workspace_id,
            qdrant_points_found=len(all_points),
            memories_recovered=len(all_points) - len(existing_mem_ids),
            memories_already_existed=len(existing_mem_ids),
            context_record_created=not context_exists,
            search_config_restored=not context_exists,
            dry_run=True,
            errors=errors,
        )

    # Step 3: Create Context record if missing
    context_record_created = False
    existing_context = await db.execute(select(Context).where(Context.id == PyUUID(context_id)))
    if existing_context.scalar_one_or_none() is None:
        context_name = request_body.context_name or f"recovered-{context_id[:8]}"
        new_context = Context(
            id=PyUUID(context_id),
            workspace_id=PyUUID(workspace_id),
            name=context_name,
            display_name=context_name,
            created_by=get_user_id(user),
        )
        db.add(new_context)
        await db.flush()
        context_record_created = True
        logger.info("context_recovered", context_id=context_id, name=context_name)

    # Step 4: Create SearchConfig if missing
    search_config_restored = False
    existing_config = await db.execute(
        select(ContextSearchConfig).where(ContextSearchConfig.context_id == PyUUID(context_id))
    )
    if existing_config.scalar_one_or_none() is None:
        new_config = ContextSearchConfig(
            context_id=PyUUID(context_id),
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
        )
        db.add(new_config)
        await db.flush()
        search_config_restored = True

    # Step 5: Reconstruct Memory records from Qdrant payloads
    memories_recovered = 0
    memories_already_existed = 0

    for point in all_points:
        mem_id = PyUUID(str(point.id))
        existing_mem = await db.execute(select(Memory.id).where(Memory.id == mem_id))
        if existing_mem.scalar_one_or_none() is not None:
            memories_already_existed += 1
            continue

        payload = point.payload or {}
        try:
            new_memory = Memory(
                id=mem_id,
                user_id=payload.get("user_id", get_user_id(user)),
                workspace_id=PyUUID(workspace_id),
                context_id=PyUUID(context_id),
                summary=payload.get("summary", ""),
                context_summary=payload.get("context_summary"),
                content=payload.get("summary", ""),  # Use summary as content fallback
                type=payload.get("type", "note"),
                importance=payload.get("importance", 0.5),
                scope=payload.get("scope", "persistent"),
                tags=payload.get("tags", []),
                embedding_status="success",  # Already in Qdrant
                client="admin-recovery",
                source="admin_recovery",
            )
            db.add(new_memory)
            memories_recovered += 1
        except Exception as e:
            errors.append(f"Failed to recover memory {mem_id}: {e!s}")

    await db.commit()

    logger.info(
        "context_recovery_complete",
        context_id=context_id,
        points_found=len(all_points),
        memories_recovered=memories_recovered,
        already_existed=memories_already_existed,
        admin_user_id=get_user_id(user),
    )

    return ContextRecoveryResponse(
        context_id=context_id,
        workspace_id=workspace_id,
        qdrant_points_found=len(all_points),
        memories_recovered=memories_recovered,
        memories_already_existed=memories_already_existed,
        context_record_created=context_record_created,
        search_config_restored=search_config_restored,
        dry_run=False,
        errors=errors,
    )
