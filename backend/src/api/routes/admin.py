"""Admin API Routes - User Management and System-wide Statistics.

Admin-only endpoints for managing users and viewing system-wide statistics.
Requires Admin role for all endpoints.
Issue #106: Refactored to use consolidated utilities
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_admin
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import (
    APIKey,
    AuditLog,
    Context,
    ContextMember,
    OAuth2Client,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from models.schemas import (
    UpdateWorkspaceSlotBonusRequest,
    UpdateWorkspaceSlotBonusResponse,
)
from utils import db_transaction, get_user_id
from utils.datetime import to_utc_iso
from utils.exceptions import (
    AuthorizationError,
    BonusBelowZeroError,
    InsufficientReasonError,
    NotFoundException,
)
from utils.logger import get_logger
from utils.plan_resolver import BASE_CAP, get_user_workspace_cap_summary

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ============================================================================
# Schemas
# ============================================================================


class UserInfo(TZAwareBaseModel):
    """User information for admin view.

    Issue #164: Extended with workspaces field.
    Issue #175: Added timezone field for user preferences.
    """

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

        # Issue #164: Apply workspace_id / plan filter
        # WorkspaceMember.user_id is not a FK to User.user_id (it stores the
        # OAuth ``sub`` claim as a plain string), so SQLAlchemy cannot infer
        # the ON clause from the schema. Pass it explicitly — otherwise the
        # join raises InvalidRequestError("Don't know how to join …").
        #
        # Both branches JOIN Workspace and filter ``deleted_at IS NULL``.
        # WorkspaceMember rows persist after a workspace is soft-deleted
        # (membership is preserved for tombstone visibility), so the
        # ``workspace_id`` filter without the soft-delete predicate would
        # surface users for a soft-deleted workspace — the same #681 class.
        #
        # ``.distinct()`` deduplicates: a user with N matching workspaces
        # would otherwise produce N rows and inflate ``total`` to count
        # workspace matches rather than distinct users, breaking pagination.
        join_filter_applied = workspace_id is not None or plan is not None
        if workspace_id:
            stmt = (
                stmt.join(WorkspaceMember, WorkspaceMember.user_id == User.user_id)
                .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                .where(
                    WorkspaceMember.workspace_id == UUID(workspace_id),
                    Workspace.deleted_at.is_(None),  # #681 pattern: soft-delete safe
                )
            )

        # Issue #164: Apply plan filter (via workspace)
        if plan:
            if not workspace_id:  # If not already joined
                stmt = stmt.join(WorkspaceMember, WorkspaceMember.user_id == User.user_id).join(
                    Workspace, Workspace.id == WorkspaceMember.workspace_id
                )
            stmt = stmt.where(
                Workspace.plan_name == plan,
                Workspace.deleted_at.is_(None),  # #681: exclude soft-deleted
            )

        # Deduplicate the user list / count when any JOIN-based filter is
        # active. A user owning K matching workspaces would otherwise
        # produce K rows in ``users_list`` and inflate ``total`` to count
        # workspace matches rather than distinct users — breaking
        # pagination and the admin list contract.
        if join_filter_applied:
            stmt = stmt.distinct()

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
                .where(
                    WorkspaceMember.user_id.in_(user_ids),
                    Workspace.deleted_at.is_(None),  # #681: exclude soft-deleted
                )
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
                        "joined_at": to_utc_iso(member.joined_at),
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
            .where(
                WorkspaceMember.user_id == user_id,
                Workspace.deleted_at.is_(None),  # #681: exclude soft-deleted
            )
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
            except (NotFoundException, AuthorizationError):
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

        # 5. Build workspace_summary (#676 admin slot bonus UI)
        # owned_count + cap come from the canonical helper so /usage/current
        # and this admin view never drift. owned_workspaces is filtered
        # inline (deleted_at IS NULL) — reusing the already-fetched
        # workspace_memberships list would conflate "owned" with "member of"
        # since membership covers all four roles, not just owner.
        from models.schemas import (
            OwnedWorkspaceInfo,
            UserDetailResponse,
            WorkspaceSummary,
        )

        owned_count, cap = await get_user_workspace_cap_summary(db, user_id)
        owned_ws_result = await db.execute(
            select(Workspace.id, Workspace.name, Workspace.plan_name)
            .where(
                Workspace.owner_user_id == user_id,
                Workspace.deleted_at.is_(None),  # #681 pattern: soft-delete safe
            )
            .order_by(Workspace.created_at.desc())
        )
        owned_ws_list = [
            OwnedWorkspaceInfo(id=str(row.id), name=row.name, plan_name=row.plan_name)
            for row in owned_ws_result.all()
        ]
        workspace_summary = WorkspaceSummary(
            owned_count=owned_count,
            workspace_slot_bonus=target_user.workspace_slot_bonus,
            base_cap=BASE_CAP,
            cap=cap,
            is_at_cap=(owned_count >= cap),
            owned_workspaces=owned_ws_list,
        )

        # 6. Build response

        return UserDetailResponse(
            user={
                "id": target_user.user_id,
                "user_id": target_user.user_id,  # Add for backward compatibility
                "email": target_user.email,
                "name": target_user.name,
                "picture": target_user.picture,
                "role": target_user.role,
                "is_initial_admin": getattr(target_user, "is_initial_admin", False),
                "created_at": to_utc_iso(target_user.created_at),
                "last_login_at": to_utc_iso(target_user.last_login_at),
                "auth_provider": target_user.auth_provider,
            },
            workspaces=workspaces,
            accessible_contexts=accessible_contexts,
            stats=stats,
            workspace_summary=workspace_summary,
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


@router.patch(
    "/users/{user_id}/workspace_slot_bonus",
    response_model=UpdateWorkspaceSlotBonusResponse,
)
async def update_workspace_slot_bonus(
    user_id: str,
    request: UpdateWorkspaceSlotBonusRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UpdateWorkspaceSlotBonusResponse:
    """Apply a signed delta to a user's workspace_slot_bonus (Admin-only).

    Issue #676. Atomic via ``UPDATE ... RETURNING`` so two admins clicking
    +1 simultaneously cannot overwrite each other (read-modify-write at the
    ORM layer would race). The DB CHECK constraint
    ``workspace_slot_bonus_nonneg`` is the ultimate safety net; the
    app-level ``BonusBelowZeroError`` exists so SDK consumers receive a
    structured 400 (``BONUS-002``) instead of a generic IntegrityError.

    ``reason`` is required only when the new cap would fall below the
    user's current owned_count — admin can still revoke bonus from users
    not at risk of over-cap without filling in a reason every time.

    All mutations write a row to ``audit_logs`` with the canonical
    ``user_metadata`` JSON payload pattern (matches
    ``system_admin_service.py``); SHA256 hashing of the integer values is
    unnecessary so they are stored as plain strings in ``old_value_hash`` /
    ``new_value_hash`` for the rare grep-by-numeric-value query.
    """
    actor_id = get_user_id(admin)

    # ---- Acquire per-user advisory lock (serializes against workspace creation) ----
    # ``QuotaService.check_workspace_creation_allowed`` (#677 sub-C)
    # acquires ``pg_advisory_xact_lock(hashtextextended('workspace_create:' || user_id, 0))``
    # before counting owned workspaces. Without holding the same lock
    # here, a concurrent workspace-create can pass its cap check while we
    # are decrementing the bonus — ending up over-cap without the admin
    # supplying a reason. The lock is xact-scoped: it is released when
    # this request's transaction commits (audit phase) or rolls back
    # (any raise path through ``get_db``).
    #
    # Bounded acquire mirrors quota_service.py:388 — ``SET LOCAL
    # lock_timeout = '5s'`` keeps a pathologically long peer transaction
    # from stalling this worker indefinitely. On SQLSTATE 55P03
    # (``lock_not_available``) we rollback the poisoned session and
    # surface a 503 (retriable); other DB errors propagate up. The reset
    # to ``'0'`` after a successful acquire prevents the 5s timeout from
    # bleeding into the subsequent SELECT/UPDATE statements.
    await db.execute(text("SET LOCAL lock_timeout = '5s'"))
    try:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(
                key=f"workspace_create:{user_id}"
            )
        )
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        # The session is poisoned until rollback — issue it before
        # raising so the next request on this session is clean.
        await db.rollback()
        if sqlstate == "55P03":
            logger.warning(
                "admin_workspace_slot_bonus_lock_timeout",
                target_user_id=user_id,
                actor_user_id=actor_id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Workspace cap lock unavailable; please retry.",
            ) from exc
        raise
    await db.execute(text("SET LOCAL lock_timeout = '0'"))

    # ---- Validation phase (read-only, outside db_transaction) ----
    # db_transaction's bare `except Exception` wraps MemoryCloudException
    # subclasses as HTTPException(500), which would convert our structured
    # BONUS-001/002 4xx errors into generic 500s. Keeping the guard raises
    # outside the transaction so they propagate to memory_cloud_exception_handler.
    target_result = await db.execute(select(User).where(User.user_id == user_id))
    target_user = target_result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    current_bonus = target_user.workspace_slot_bonus
    projected_after = current_bonus + request.delta

    if projected_after < 0:
        raise BonusBelowZeroError(current=current_bonus, delta=request.delta)

    # owned_count is read before the mutation; the cap below is the
    # *new* cap (BASE_CAP + projected_after). Computing it locally avoids a
    # second query and keeps the destructive-op decision deterministic.
    owned_count, _current_cap = await get_user_workspace_cap_summary(db, user_id)
    projected_cap = BASE_CAP + projected_after

    reason_clean = (request.reason or "").strip() or None
    is_destructive = request.delta < 0 and projected_cap < owned_count
    if is_destructive and reason_clean is None:
        raise InsufficientReasonError()

    target_email = target_user.email  # capture for audit before leaving the SELECT scope

    # ---- Mutation phase ----
    # Atomic delta application — UPDATE ... RETURNING is race-free with
    # respect to concurrent +1/-1 from another admin session.
    #
    # The `+ delta >= 0` guard in the WHERE clause is the race-safe form
    # of BONUS-002: if a concurrent decrement landed between the
    # validation phase above and this UPDATE such that the live value
    # would go negative, the UPDATE matches zero rows instead of tripping
    # the workspace_slot_bonus_nonneg CHECK at COMMIT (which would surface
    # as IntegrityError → generic 500, masking the structured 400). We
    # disambiguate 404 (user vanished) from raced-BONUS-002 (user exists,
    # would-go-negative) via a single follow-up SELECT in the rare error
    # path. The raise paths sit OUTSIDE db_transaction so
    # MemoryCloudException subclasses propagate to
    # memory_cloud_exception_handler instead of being swallowed as 500.
    update_stmt = (
        update(User)
        .where(
            User.user_id == user_id,
            User.workspace_slot_bonus + request.delta >= 0,
        )
        .values(workspace_slot_bonus=User.workspace_slot_bonus + request.delta)
        .returning(User.workspace_slot_bonus)
    )
    update_result = await db.execute(update_stmt)
    returned = update_result.one_or_none()
    if returned is None:
        live_bonus = await db.scalar(
            select(User.workspace_slot_bonus).where(User.user_id == user_id)
        )
        if live_bonus is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        raise BonusBelowZeroError(current=int(live_bonus), delta=request.delta)

    after_value = returned.workspace_slot_bonus
    # Derive before_value from the authoritative RETURNING result so that
    # under concurrent +1 collisions the audit trail records the actual
    # pre-state of THIS update, not a stale SELECT snapshot.
    before_value = after_value - request.delta
    final_cap = BASE_CAP + after_value

    # Race-guard the destructive-op check post-UPDATE. The pre-UPDATE
    # validation used ``current_bonus`` from a snapshot, so two admins
    # concurrently decrementing bonus on a user at ``cap == owned`` could
    # both pass the "not destructive yet" gate independently — but the
    # second commit lands the user in an over-cap state without anyone
    # supplying a reason. Re-checking against the authoritative
    # ``after_value`` from RETURNING closes the race: if the post-state is
    # over-cap and no reason was supplied, we raise ``BONUS-001`` here
    # (still outside ``db_transaction``), and ``get_db``'s except handler
    # rolls back the staged UPDATE on its way out. No audit row is
    # written for the rolled-back attempt — admin sees a 400 and can
    # retry with a reason.
    if request.delta < 0 and final_cap < owned_count and reason_clean is None:
        raise InsufficientReasonError()

    # Audit write + commit are transaction-wrapped so a failure here
    # rolls back the staged UPDATE above (single SQLAlchemy session
    # transaction across both operations).
    async with db_transaction(
        db, "update_workspace_slot_bonus", "Failed to update workspace slot bonus"
    ):
        audit = AuditLog(
            user_email=admin.get("email", actor_id),
            user_id=actor_id,
            action="workspace_slot_bonus_update",
            resource=f"user:{target_email}",
            old_value_hash=str(before_value),
            new_value_hash=str(after_value),
            user_metadata={
                "actor_user_id": actor_id,
                "target_user_id": user_id,
                "before_value": before_value,
                "after_value": after_value,
                "delta": request.delta,
                "reason": reason_clean,
            },
        )
        db.add(audit)
        await db.commit()

    logger.info(
        "admin_update_workspace_slot_bonus",
        actor_user_id=actor_id,
        target_user_id=user_id,
        before=before_value,
        after=after_value,
        delta=request.delta,
        destructive=is_destructive,
    )

    return UpdateWorkspaceSlotBonusResponse(
        before_value=before_value,
        after_value=after_value,
        owned_count=owned_count,
        base_cap=BASE_CAP,
        cap=final_cap,
        is_at_cap=(owned_count >= final_cap),
        reason=reason_clean,
    )


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
# Account Erasure (Issue #360, GDPR Art.17 / APPI compliance)
# ============================================================================


from typing import Literal  # noqa: E402  (kept local to the new endpoint)

# Admin reason codes — explicitly excludes ``self_service`` (which is
# locked to the self-service flow by the DB CHECK constraint). Pydantic
# now rejects bad values at the API boundary (422) rather than letting
# them reach the service layer.
AdminErasureReasonCode = Literal[
    "user_request_via_support",
    "legal_order",
    "inactivity_policy",
    "abuse_violation",
    "other",
]


class AdminErasureRequestBody(BaseModel):
    """Payload for POST /admin/users/{user_id}/erase.

    ``reason_detail`` is free-form admin notes bounded to 1000 chars at
    the service layer (matches the ``valid_erasure_*`` CHECK constraint
    in the migration).
    """

    reason_code: AdminErasureReasonCode
    reason_detail: str | None = None


@router.post("/users/{user_id}/erase")
async def admin_force_erase_user(
    user_id: str,
    body: AdminErasureRequestBody,
    request: Request,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Force-erase a user account (admin-only, GDPR Art.17 / APPI compliance).

    Skips the 7-day cooling-off period: the deletion executes immediately.
    Cross-store cleanup (Stripe + Qdrant + Postgres + Redis) is handled
    by AccountErasureService following the same orchestration as the
    self-service path.

    Required body:
        reason_code: one of user_request_via_support / legal_order /
            inactivity_policy / abuse_violation / other
        reason_detail: free-form admin note (max 1000 chars)

    Protections (mirrors SystemAdminService.can_delete_admin):
        - 403 if target is the initial admin
        - 403 if target is the last remaining admin
        - 403 if admin tries to erase their own account
        - 409 if an erasure request is already in flight for this user
        - 409 if the target owns a shared workspace and no alternate
          admin exists for auto-transfer

    This endpoint replaces the older `DELETE /admin/users/{user_id}` for
    new integrations. The old route is kept temporarily for backward
    compatibility but does NOT clean Qdrant or all OAuth artifacts.
    """
    from services.account_erasure_service import AccountErasureService

    admin_id = get_user_id(admin)

    if admin_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot erase your own account via the admin path",
        )

    service = AccountErasureService(db)
    record = await service.admin_force_erase(
        target_user_id=user_id,
        initiator_user_id=admin_id,
        reason_code=body.reason_code,
        reason_detail=body.reason_detail,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    return {
        "request_id": str(record.id),
        "status": record.status,
        "deleted_data_summary": record.deleted_data_summary,
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
    import re
    from uuid import UUID as PyUUID

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    from config.settings import get_settings
    from db.qdrant import KAGURA_MEMORIES_COLLECTION, get_qdrant_client
    from models.config import ContextSearchConfig

    context_id = request_body.context_id

    # Validate context_id is a valid UUID (before any external calls)
    try:
        PyUUID(context_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid context_id format") from e

    settings = get_settings()
    client = get_qdrant_client()
    collection_name = KAGURA_MEMORIES_COLLECTION
    errors: list[str] = []

    # Scroll Qdrant for all points with this context_id (capped at 10k)
    max_points = 10_000
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
        if next_offset is None or len(all_points) >= max_points:
            if len(all_points) > max_points:
                all_points = all_points[:max_points]
                errors.append(f"Capped at {max_points} points. Context may have more.")
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

    # Validate workspace_id UUID format
    try:
        workspace_id = str(PyUUID(workspace_id))
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    if request_body.dry_run:
        # Just report what we found
        # Check if context record exists
        existing_context = await db.execute(select(Context).where(Context.id == PyUUID(context_id)))
        context_exists = existing_context.scalar_one_or_none() is not None

        # Batch-check existing memory records (avoid N+1)
        point_ids = [PyUUID(str(p.id)) for p in all_points]
        existing_result = await db.execute(select(Memory.id).where(Memory.id.in_(point_ids)))
        existing_mem_ids = {row[0] for row in existing_result.all()}

        # Check if search config exists independently of context
        existing_cfg = await db.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == PyUUID(context_id))
        )
        config_exists = existing_cfg.scalar_one_or_none() is not None

        return ContextRecoveryResponse(
            context_id=context_id,
            workspace_id=workspace_id,
            qdrant_points_found=len(all_points),
            memories_recovered=len(all_points) - len(existing_mem_ids),
            memories_already_existed=len(existing_mem_ids),
            context_record_created=not context_exists,
            search_config_restored=not config_exists,
            dry_run=True,
            errors=errors,
        )

    # Step 3: Create Context record if missing
    context_record_created = False
    existing_context = await db.execute(select(Context).where(Context.id == PyUUID(context_id)))
    if existing_context.scalar_one_or_none() is None:
        raw_name = request_body.context_name or f"recovered-{context_id[:8]}"
        # Sanitize name to match DB constraint ^[a-z0-9_-]+$
        context_name = re.sub(r"[^a-z0-9_-]", "-", raw_name.lower()).strip("-")
        if not context_name:
            context_name = f"recovered-{context_id[:8]}"
        new_context = Context(
            id=PyUUID(context_id),
            workspace_id=PyUUID(workspace_id),
            name=context_name,
            display_name=raw_name,
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
    # Batch-check existing memory IDs (avoid N+1)
    point_ids = [PyUUID(str(p.id)) for p in all_points]
    existing_ids_result = await db.execute(select(Memory.id).where(Memory.id.in_(point_ids)))
    existing_ids = {row[0] for row in existing_ids_result.all()}

    memories_recovered = 0
    memories_already_existed = 0

    for point in all_points:
        mem_id = PyUUID(str(point.id))
        if mem_id in existing_ids:
            memories_already_existed += 1
            continue

        payload = point.payload or {}
        try:
            new_memory = Memory(
                id=mem_id,
                summary_embedding_id=mem_id,  # Qdrant point ID
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
        except (ValueError, KeyError, TypeError) as e:
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
