"""Admin API Routes - User Management and System-wide Statistics.

Admin-only endpoints for managing users and viewing system-wide statistics.
Requires Admin role for all endpoints.
Issue #106: Refactored to use consolidated utilities
"""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_admin
from auth.workspace_roles import ContextRole, WorkspaceRole
from config.settings import get_settings
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import (
    APIKey,
    AuditLog,
    Context,
    ContextMember,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from models.schemas import (
    UpdateWorkspaceSlotBonusRequest,
    UpdateWorkspaceSlotBonusResponse,
    UserWorkspaceInfo,
)
from services.email_service import get_email_service
from services.workspace_ownership_service import WorkspaceOwnershipService
from utils import db_transaction, get_user_id
from utils.datetime import to_utc_iso
from utils.db_helpers import is_deadlock, is_lock_not_available
from utils.exceptions import (
    AdminProtectionError,
    AuthorizationError,
    BadRequestError,
    BonusBelowZeroError,
    InsufficientReasonError,
    NotFoundException,
    ValidationError,
)
from utils.logger import get_logger
from utils.plan_resolver import (
    BASE_CAP,
    get_user_workspace_cap_summary,
    resolve_workspace_cap,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ============================================================================
# Schemas
# ============================================================================


class UserInfo(TZAwareBaseModel):
    """User information for admin view.

    Issue #164: Extended with workspaces field.
    Issue #175: Added timezone field for user preferences.
    Issue #695: Added owned_count / workspace_slot_bonus / base_cap / cap so
    the /admin/users list can surface each user's owned-workspace usage
    against their cap without a follow-up per-user detail fetch. Field names
    intentionally mirror ``WorkspaceSummary`` (the detail endpoint's payload)
    so client code and mental models stay consistent. Defaults keep the
    schema additive — older consumers that ignore these fields still work.
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
    # Issue #164: Workspace memberships. Typed since #1754 — the same model the
    # detail endpoint returns, so the list cannot silently drop a field again.
    workspaces: list[UserWorkspaceInfo] = []

    # Issue #695: owned-workspace cap summary (mirrors WorkspaceSummary fields
    # used in /admin/users/{id} detail — same field names ``base_cap`` /
    # ``cap`` so client code and mental models stay consistent across
    # closely-related admin endpoints). Always populated for active users so
    # the list UI can render `owned_count / cap` and an at-cap flag.
    # ``cap`` defaults to ``BASE_CAP`` (not a literal ``1``) so the schema
    # stays in sync if ``BASE_CAP`` ever changes — resolved per-user in the
    # handler via ``plan_resolver.resolve_workspace_cap`` (#1550: base +
    # slot bonus + highest owned tier's grant).
    owned_count: int = 0
    workspace_slot_bonus: int = 0
    base_cap: int = BASE_CAP
    cap: int = BASE_CAP

    # Issue #246: current_context_id removed
    # current_context_id: str | None = None
    # current_context_name: str | None = None
    # current_context_display_name: str | None = None


class UserStatsUser(BaseModel):
    """The account a ``UserStats`` response describes.

    Issue #1882: typed so the OpenAPI public-id guard covers it, and carries
    the string ``user_id`` instead of the integer ``users`` PK (#1813).
    """

    user_id: str
    email: str
    name: str | None
    role: str


class UserStats(BaseModel):
    """Detailed statistics for a specific user."""

    user: UserStatsUser
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


def _apply_user_list_filters(
    stmt: Any,
    *,
    search: str | None,
    role: str | None,
    workspace_id: str | None,
    plan: str | None,
) -> tuple[Any, bool]:
    """Apply the admin user-list filters (#164), returning (stmt, joined).

    ``WorkspaceMember.user_id`` is not a FK to ``User.user_id`` (it stores the
    OAuth ``sub`` claim as a plain string), so SQLAlchemy cannot infer the ON
    clause — it is passed explicitly or the join raises
    ``InvalidRequestError("Don't know how to join …")``.

    Both join branches also filter ``Workspace.deleted_at IS NULL``:
    ``WorkspaceMember`` rows persist after a workspace is soft-deleted
    (membership is kept for tombstone visibility), so a bare ``workspace_id``
    filter would surface users of a soft-deleted workspace — the #681 class.

    Returns:
        The filtered statement, and whether a JOIN-based filter was applied.
        The caller needs the flag for ``.distinct()``: without it a user owning
        K matching workspaces produces K rows and inflates ``total`` into a
        count of workspace matches, breaking pagination.
    """
    from uuid import UUID

    if search:
        search_pattern = f"%{search}%"
        stmt = stmt.where(or_(User.email.ilike(search_pattern), User.name.ilike(search_pattern)))

    if role:
        stmt = stmt.where(User.role == role)

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

    if plan:
        if not workspace_id:  # not already joined
            stmt = stmt.join(WorkspaceMember, WorkspaceMember.user_id == User.user_id).join(
                Workspace, Workspace.id == WorkspaceMember.workspace_id
            )
        stmt = stmt.where(
            Workspace.plan_name == plan,
            Workspace.deleted_at.is_(None),  # #681: exclude soft-deleted
        )

    return stmt, join_filter_applied


async def _fetch_memory_counts(db: AsyncSession, user_ids: list[str]) -> dict[str, dict[str, int]]:
    """Live memory count per user, in ONE query (#164 N+1 fix)."""
    if not user_ids:
        return {}
    result = await db.execute(
        select(Memory.user_id, func.count(Memory.id).label("memory_count"))
        .where(Memory.user_id.in_(user_ids), Memory.deleted_at.is_(None))
        .group_by(Memory.user_id)
    )
    return {row.user_id: {"memory_count": row.memory_count or 0} for row in result.all()}


async def _fetch_workspace_caps(db: AsyncSession, user_ids: list[str]) -> dict[str, dict[str, int]]:
    """Owned-workspace count + slot bonus + cap per user, in ONE query (#695).

    Mirrors the SELECT in ``plan_resolver.get_user_workspace_cap_summary`` but
    joined for every listed user — calling the per-user helper in the render
    loop would re-introduce the N+1 pattern fixed for memory counts. The cap
    itself still comes from ``plan_resolver.resolve_workspace_cap`` (#1550:
    the owned plan names ride the same JOIN) so the list and the detail view
    never drift. LEFT OUTER JOIN so users with zero owned workspaces still
    surface (count = 0), and the join predicate excludes soft-deleted
    workspaces (#681: cap math counts only live workspaces).
    """
    if not user_ids:
        return {}
    result = await db.execute(
        select(
            User.user_id,
            User.workspace_slot_bonus,
            func.count(Workspace.id).label("owned_count"),
            func.array_agg(Workspace.plan_name).label("owned_plan_names"),
        )
        .outerjoin(
            Workspace,
            and_(
                Workspace.owner_user_id == User.user_id,
                Workspace.deleted_at.is_(None),
            ),
        )
        .where(User.user_id.in_(user_ids))
        .group_by(User.id, User.user_id, User.workspace_slot_bonus)
    )
    caps: dict[str, dict[str, int]] = {}
    for row in result.all():
        summary = resolve_workspace_cap(
            int(row.owned_count or 0),
            int(row.workspace_slot_bonus or 0),
            row.owned_plan_names or (),
        )
        caps[row.user_id] = {
            "owned_count": summary.owned_count,
            "workspace_slot_bonus": summary.slot_bonus,
            "base_cap": summary.base,
            "cap": summary.cap,
        }
    return caps


async def _attach_user_workspaces(db: AsyncSession, user_infos: list, users_list: list) -> None:
    """Fill each response row's ``workspaces`` list, in ONE query (#1456).

    Soft-deleted workspaces are excluded (#681). Users with no live membership
    get an empty list rather than being skipped, so the field is always present.
    """
    user_ids = [u.user_id for u in users_list]
    result = await db.execute(
        select(WorkspaceMember, Workspace)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(
            WorkspaceMember.user_id.in_(user_ids),
            Workspace.deleted_at.is_(None),  # #681: exclude soft-deleted
        )
        .order_by(WorkspaceMember.role.desc())
    )

    by_user_id = {u.user_id: u for u in users_list}
    user_workspaces_map: dict[str, list[UserWorkspaceInfo]] = {}
    for member, workspace in result.all():
        user_obj = by_user_id.get(member.user_id)
        user_workspaces_map.setdefault(member.user_id, []).append(
            UserWorkspaceInfo(
                workspace_id=str(workspace.id),
                workspace_name=workspace.name,
                role=member.role,
                is_primary=bool(user_obj and user_obj.current_workspace_id == workspace.id),
                joined_at=member.joined_at,
                plan_name=workspace.plan_name,  # #1754: shown on the list's badges
            )
        )

    for user_info in user_infos:
        user_info.workspaces = user_workspaces_map.get(user_info.id, [])


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
        plan: Filter by plan tier (free/basic/pro/promax)
        sort: Sort field (created_at, last_login, memory_count)

    Returns:
        List of users with memory counts, storage usage, and optionally workspace info
    """
    try:
        stmt, join_filter_applied = _apply_user_list_filters(
            select(User), search=search, role=role, workspace_id=workspace_id, plan=plan
        )
        # Deduplicate the user list / count when any JOIN-based filter is
        # active — see the helper's docstring for why total breaks without it.
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

        user_ids = [u.user_id for u in users_list]
        memory_stats_dict = await _fetch_memory_counts(db, user_ids)
        cap_stats_dict = await _fetch_workspace_caps(db, user_ids)

        # Issue #246: current_context_id removed - skip context lookup
        # from models.auth import Context
        # context_map = {}
        # user_ids_with_context = [u.user_id for u in users_list if u.current_context_id]

        # Build user info list
        user_infos = []
        for u in users_list:
            stats = memory_stats_dict.get(u.user_id, {"memory_count": 0})
            # Issue #695: cap_stats falls back to "0 owned, no tier" for users
            # that returned no row (extremely unlikely — only if the user was
            # deleted between the listing query and the cap query). Same
            # shape as the User column default so the UI always has values.
            if (cap := cap_stats_dict.get(u.user_id)) is None:
                fallback = resolve_workspace_cap(0, int(u.workspace_slot_bonus or 0), ())
                cap = {
                    "owned_count": fallback.owned_count,
                    "workspace_slot_bonus": fallback.slot_bonus,
                    "base_cap": fallback.base,
                    "cap": fallback.cap,
                }
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
                    owned_count=cap["owned_count"],  # Issue #695
                    workspace_slot_bonus=cap["workspace_slot_bonus"],  # Issue #695
                    base_cap=cap["base_cap"],  # Issue #695
                    cap=cap["cap"],  # Issue #695
                    # Issue #246: current_context_id removed
                    # current_context_id=context_info.get('context_id'),
                    # current_context_name=context_info.get('context_name'),
                    # current_context_display_name=context_info.get('context_display_name'),
                )
            )

        # Issue #164: Load workspace memberships if requested
        if include_workspaces and users_list:
            await _attach_user_workspaces(db, user_infos, users_list)

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
            select(func.count(Memory.id)).where(
                Memory.user_id == user_id, Memory.deleted_at.is_(None)
            )
        )
        total_count = total_count_result.scalar() or 0

        # Count by scope (working vs persistent)
        working_count_result = await db.execute(
            select(func.count(Memory.id)).where(
                and_(
                    Memory.user_id == user_id,
                    Memory.scope == "working",
                    Memory.deleted_at.is_(None),
                )
            )
        )
        working_count = working_count_result.scalar() or 0

        persistent_count = total_count - working_count

        # Count by type
        type_result = await db.execute(
            select(Memory.type, func.count(Memory.id))
            .where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
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
            user=UserStatsUser(
                user_id=target_user.user_id,
                email=target_user.email,
                name=target_user.name,
                role=target_user.role or "user",
            ),
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

        for member, workspace in workspace_memberships:
            try:
                contexts = await perm_service.get_accessible_contexts(user_id, workspace.id)
                for ctx in contexts:
                    all_contexts.append(ctx)
                    context_workspace_map[ctx.id] = (workspace, member.role)
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
            ctx_role: WorkspaceRole | ContextRole = ContextRole.VIEWER  # Default
            if workspace_role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
                ctx_role = workspace_role  # #699: preserve owner/admin distinction
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
            select(func.count(Memory.id)).where(
                Memory.user_id == user_id, Memory.deleted_at.is_(None)
            )
        )
        total_memories = total_memories_result.scalar() or 0

        working_memories_result = await db.execute(
            select(func.count(Memory.id)).where(
                and_(
                    Memory.user_id == user_id,
                    Memory.scope == "working",
                    Memory.deleted_at.is_(None),
                )
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

        cap_summary = await get_user_workspace_cap_summary(db, user_id)
        owned_count, cap = cap_summary.owned_count, cap_summary.cap
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
            base_cap=cap_summary.base,
            tier_grant=cap_summary.tier_grant,  # #1550
            tier=cap_summary.tier,  # #1550
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
        raise AdminProtectionError("Cannot change your own admin role", reason="self_demotion")

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
    # *new* cap — the resolved cap moves 1:1 with the bonus, so shifting it
    # by ``delta`` avoids a second query without restating the formula
    # (#1550: the tier grant is already inside ``cap_summary.cap``) and
    # keeps the destructive-op decision deterministic.
    cap_summary = await get_user_workspace_cap_summary(db, user_id)
    owned_count = cap_summary.owned_count
    projected_cap = cap_summary.cap + request.delta

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
    # Re-base the resolved cap on the authoritative post-UPDATE bonus (the
    # snapshot bonus may be stale under a concurrent admin edit).
    final_cap = cap_summary.cap - cap_summary.slot_bonus + after_value

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
        base_cap=cap_summary.base,
        cap=final_cap,
        is_at_cap=(owned_count >= final_cap),
        reason=reason_clean,
    )


class UserDeleteRequest(BaseModel):
    """Explicit successors for administrative deletion (#2372).

    Workspace successors also receive contexts created in that workspace;
    context_transfers overrides that choice for individual contexts.
    """

    workspace_transfers: dict[UUID, str] = Field(default_factory=dict)
    context_transfers: dict[UUID, str] = Field(default_factory=dict)


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: UserDeleteRequest | None = None,
):
    """Delete personal data after explicitly transferring ownership (Admin-only).

    Returns 409 without modifying data when an owned workspace or created
    shared context lacks an eligible successor, or when an owned workspace is
    under a running subscription (``BILLING-005``, #1940). A private context
    passes to a linked account that wrote in it, otherwise it is deleted with
    the user (#2417). A soft-deleted workspace the user owns takes no
    successor: it is hard-deleted with the user, everything in it included,
    and listed under ``purged_workspaces`` (#2406). Initial/last admin and
    self-deletion protections remain in force. The transfer and deletion
    commit together.
    """
    from services.ownership_notifications import notify_new_owner_best_effort
    from services.system_admin_service import SystemAdminService
    from services.user_deletion_service import (
        delete_user_rows,
        finish_user_deletion,
        transfer_user_ownership,
    )

    admin_id = get_user_id(admin)
    if admin_id == user_id:
        raise AdminProtectionError("Cannot delete your own account", reason="self_deletion")
    service = SystemAdminService(db)
    can_delete, reason = await service.can_delete_admin(user_id)
    if not can_delete:
        raise AdminProtectionError(reason, reason="admin_protected")
    request = request or UserDeleteRequest()
    try:
        transferred = await transfer_user_ownership(
            db,
            user_id,
            workspace_transfers=request.workspace_transfers,
            context_transfers=request.context_transfers,
            actor_email=admin.get("email") or "admin@local",
            actor_user_id=admin_id,
        )
        result = await delete_user_rows(
            db,
            user_id,
            actor_email=admin.get("email") or "admin@local",
            actor_user_id=admin_id,
        )
        await db.commit()
    except DBAPIError as exc:
        await db.rollback()
        # #2418: a deadlock victim (40P01) is the same "another operation
        # holds these rows" as a NOWAIT conflict (55P03): nothing committed,
        # the deletion can simply run again.
        if is_lock_not_available(exc) or is_deadlock(exc):
            raise HTTPException(409, "Workspace is being modified; retry deletion") from exc
        raise
    except Exception:
        await db.rollback()
        raise
    await finish_user_deletion(result)
    for workspace_id, successor in transferred.items():
        await notify_new_owner_best_effort(db, workspace_id, successor)
    logger.info(
        "admin_delete_user",
        user_id=user_id,
        admin_id=admin_id,
        memories_deleted=result.memories,
        api_keys_deleted=result.api_keys,
        contexts_deleted=result.contexts,
        connector_api_keys_reowned=result.connector_api_keys,
        workspaces_transferred=[str(ws_id) for ws_id in transferred],
        workspaces_purged=[str(ws.id) for ws in result.purged_workspaces],
    )
    return {
        "message": "User deleted successfully",
        "user_id": user_id,
        "deleted": {
            "memories": result.memories,
            "api_keys": result.api_keys,
            "contexts": result.contexts,
        },
        "reowned": {"connector_api_keys": result.connector_api_keys},
        "transferred_workspaces": {str(k): v for k, v in transferred.items()},
        "purged_workspaces": [ws.summary() for ws in result.purged_workspaces],
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

    Vector store unavailable (#2282): 503 ``ERASURE-007`` when the
    vector-store delete (the first step) fails. Nothing was erased; the
    request is kept as ``cooling_off`` and the hourly sweep runs it again
    after ``details.scheduled_for``. ``details`` carries ``request_id``,
    ``attempt``, ``max_attempts``, ``scheduled_for`` and ``error_class``.
    After the last attempt the request is ``failed``
    (``vector_store_unavailable after N attempts``) and the error is the
    vector store's own (502).

    Rows busy (#2418): the same 503 ``ERASURE-007`` when the workspace
    hand-over or the Postgres step meets a row lock another operation holds
    (``error_class`` ``LockNotAvailable`` or ``DeadlockDetected``); that step
    is rolled back and the request rescheduled on the same attempt budget.
    After the last attempt the request is ``failed``
    (``lock_conflict after N attempts``) and the error is 409 ``RES-002``.

    This endpoint replaces the older `DELETE /admin/users/{user_id}` for
    new integrations. The old route is kept temporarily for backward
    compatibility. It does not remove all OAuth artifacts, and its vector
    cleanup is not a cascade: it deletes the points of the contexts it
    deletes after the commit, best-effort, and the daily
    ``sweep_orphan_vectors`` removes every point it leaves (the user's own
    memories in surviving contexts, a failed call).
    """
    from services.account_erasure_service import AccountErasureService

    admin_id = get_user_id(admin)

    if admin_id == user_id:
        raise AdminProtectionError(
            "Cannot erase your own account via the admin path", reason="self_erasure"
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
# Break-glass ownership force-transfer (Issue #1101)
# ============================================================================


class ForceTransferOwnershipRequest(BaseModel):
    """Break-glass force-transfer request (#1101).

    ``reason`` is REQUIRED (non-empty) — a privileged ownership override with no
    justification is a red flag; it is recorded verbatim in the mandatory audit.
    """

    target_user_id: str = Field(..., min_length=1, max_length=255)
    reason: str = Field(
        ...,
        min_length=3,
        max_length=1000,
        description="Why ownership is being force-transferred (required, audited).",
    )


@router.post("/workspaces/{workspace_id}/force-transfer-ownership")
async def force_transfer_workspace_ownership(
    workspace_id: str,
    body: ForceTransferOwnershipRequest,
    response: Response,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Break-glass: a system admin force-transfers workspace ownership (#1101).

    For when the current owner is unavailable. System-admin-gated
    (``require_admin``), bound to the PATH ``workspace_id``, serialized on the
    shared workspace row lock, bumps ``ownership_epoch`` (invalidates the displaced
    owner's billing-handoff tokens / sessions — #1100), and writes a MANDATORY
    audit row (``break_glass`` + ``reason`` + acting admin) atomically with the
    transfer. The target need not be a member — a non-member is added as OWNER,
    which handles the zero-member workspace. The displaced previous owner is
    notified best-effort.

    Dual-control (#1113): when ``require_dual_control_force_transfer`` is enabled,
    this does NOT seize ownership immediately — it files a PENDING request and
    returns ``202`` with a ``request_id`` that a SECOND, distinct system admin
    must approve via ``/admin/force-transfer-requests/{request_id}/approve``.
    Default config keeps the immediate single-control behavior.
    """
    admin_id = get_user_id(admin)
    try:
        ws_uuid = UUID(workspace_id)
    except ValueError as exc:
        raise ValidationError("Invalid workspace_id", field="workspace_id") from exc

    svc = WorkspaceOwnershipService(db)
    admin_email = str(admin.get("email") or admin_id)

    # Dual-control: file a pending request for second-admin approval (202).
    if get_settings().require_dual_control_force_transfer:
        req = await svc.initiate_force_transfer(
            workspace_id=ws_uuid,
            target_user_id=body.target_user_id,
            performed_by_user_id=admin_id,
            performed_by_email=admin_email,
            reason=body.reason,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return {
            "workspace_id": str(req.workspace_id),
            "request_id": str(req.id),
            "status": "pending_approval",
            "target_user_id": req.target_user_id,
            "initiated_by": req.initiated_by_user_id,
            "dual_control": True,
        }

    result = await svc.force_transfer_ownership(
        workspace_id=ws_uuid,
        target_user_id=body.target_user_id,
        performed_by_user_id=admin_id,
        performed_by_email=admin_email,
        reason=body.reason,
    )

    # Notify the displaced previous owner (#1101) — only on an actual change, and
    # best-effort: the transfer already committed, so a notification failure must
    # never surface to the admin caller.
    if result.changed:
        await _notify_force_transfer_best_effort(db, ws_uuid, result.previous_owner_id)

    return {
        "workspace_id": str(result.workspace_id),
        "previous_owner_id": result.previous_owner_id,
        "new_owner_id": result.new_owner_id,
        "ownership_epoch": result.ownership_epoch,
        "changed": result.changed,
        "dual_control": False,
    }


@router.post("/force-transfer-requests/{request_id}/approve")
async def approve_force_transfer_request(
    request_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Dual-control (#1113): a SECOND system admin approves a pending force-transfer.

    System-admin-gated. The approver MUST be a different admin than the initiator
    (enforced in the service → 400). On approval the seizure applies atomically
    with a mandatory audit recording BOTH identities, bumps ``ownership_epoch``,
    and the displaced previous owner is notified best-effort. Refused with 409 if
    the request is not pending, or if the workspace's ownership moved since the
    request was filed (stale — re-initiate).
    """
    admin_id = get_user_id(admin)
    try:
        req_uuid = UUID(request_id)
    except ValueError as exc:
        raise ValidationError("Invalid request_id", field="request_id") from exc

    result = await WorkspaceOwnershipService(db).approve_force_transfer(
        request_id=req_uuid,
        approver_user_id=admin_id,
        approver_email=str(admin.get("email") or admin_id),
    )

    if result.changed:
        await _notify_force_transfer_best_effort(db, result.workspace_id, result.previous_owner_id)

    return {
        "workspace_id": str(result.workspace_id),
        "request_id": request_id,
        "status": "approved",
        "previous_owner_id": result.previous_owner_id,
        "new_owner_id": result.new_owner_id,
        "ownership_epoch": result.ownership_epoch,
        "changed": result.changed,
    }


@router.post("/force-transfer-requests/{request_id}/cancel")
async def cancel_force_transfer_request(
    request_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Dual-control (#1113): retract a pending force-transfer request.

    System-admin-gated. Lets an admin cancel a pending request (e.g. filed in
    error) without having to initiate a fresh one to supersede it. Refused with
    409 if the request is not pending.
    """
    admin_id = get_user_id(admin)
    try:
        req_uuid = UUID(request_id)
    except ValueError as exc:
        raise ValidationError("Invalid request_id", field="request_id") from exc

    req = await WorkspaceOwnershipService(db).cancel_force_transfer(
        request_id=req_uuid,
        cancelled_by_user_id=admin_id,
        cancelled_by_email=str(admin.get("email") or admin_id),
    )
    return {
        "workspace_id": str(req.workspace_id),
        "request_id": str(req.id),
        "status": req.status,
        "cancelled_by": admin_id,
    }


@router.get("/force-transfer-requests/{request_id}")
async def get_force_transfer_request(
    request_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Dual-control (#1113): inspect a force-transfer request before approving.

    System-admin-gated read so the SECOND admin can verify the target, reason,
    and initiator before calling ``/approve`` — making four-eyes an informed
    review rather than a blind second button-press. 404 if no such request.
    """
    # ``admin`` is the system-admin auth gate (require_admin); the read itself
    # needs no actor identity.
    _ = get_user_id(admin)
    try:
        req_uuid = UUID(request_id)
    except ValueError as exc:
        raise ValidationError("Invalid request_id", field="request_id") from exc

    req = await WorkspaceOwnershipService(db).get_force_transfer_request(request_id=req_uuid)
    return {
        "request_id": str(req.id),
        "workspace_id": str(req.workspace_id),
        "target_user_id": req.target_user_id,
        "reason": req.reason,
        "status": req.status,
        "initiated_by_user_id": req.initiated_by_user_id,
        "initiated_by_email": req.initiated_by_email,
        "ownership_epoch_at_initiation": req.ownership_epoch_at_initiation,
        "created_at": to_utc_iso(req.created_at),
        "decided_by_user_id": req.decided_by_user_id,
        "decided_at": to_utc_iso(req.decided_at) if req.decided_at else None,
    }


async def _notify_force_transfer_best_effort(
    db: AsyncSession, workspace_id: UUID, previous_owner_id: str
) -> None:
    """Best-effort notify the displaced previous owner (#1101).

    The transfer is already committed, so the WHOLE block is guarded — neither the
    resolution queries nor the (no-raise) EmailService call may surface to the
    admin caller. A previous owner without an email is simply skipped.
    """
    try:
        email = (
            await db.execute(select(User.email).where(User.user_id == previous_owner_id))
        ).scalar_one_or_none()
        if not email:
            return
        name = (
            await db.execute(select(Workspace.name).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        await get_email_service().send_workspace_ownership_force_transferred(
            to_email=email,
            workspace_name=name or "your workspace",
        )
    except Exception as exc:
        # Best-effort — swallow everything (resolution query or provider error). Log
        # the exception TYPE only (a SQLAlchemy error string echoes bound params).
        # Include previous_owner_id so ops can reconcile / hand-deliver the dropped
        # security-transparency notice (it is a user id, not PII like the email).
        logger.warning(
            "force_transfer_notify_failed",
            workspace_id=str(workspace_id),
            previous_owner_id=previous_owner_id,
            error_type=type(exc).__name__,
        )


# ============================================================================
# Embedding Retry (Issue #93)
# ============================================================================


@router.post("/embedding/retry-failed")
async def retry_failed_embeddings(
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    context_id: str | None = Query(None, description="Filter by context ID (UUID)"),
    workspace_id: str | None = Query(None, description="Filter by workspace ID (UUID)"),
    include_erased: bool = Query(
        False, description="Explicitly restore scrubbed erased-author memories"
    ),
) -> dict:
    """Reset failed embeddings to pending for automatic retry.

    Erased-author survivors are excluded by default. ``include_erased=true``
    explicitly restores them under their pseudonym with scrubbed details.
    """
    from uuid import UUID as PyUUID

    from sqlalchemy import case, func, update

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

    if include_erased:
        from db.point_writer_lock import hold_point_writer_lock

        await hold_point_writer_lock(db)

    conditions = [
        Memory.embedding_status == "failed",
        Memory.deleted_at.is_(None),
    ]
    if not include_erased:
        conditions.append(Memory.embedding_suppressed_at.is_(None))
    if context_id:
        conditions.append(Memory.context_id == context_id)
    if workspace_id:
        conditions.append(Memory.workspace_id == workspace_id)

    stmt = (
        update(Memory)
        .where(
            Memory.id.in_(
                select(Memory.id)
                .where(*conditions)
                .order_by(Memory.id)
                .with_for_update(key_share=True)
            )
        )
        # #979: reset the auto-retry budget too — this manual endpoint is the
        # override for rows that exhausted MAX_EMBEDDING_RETRIES, so a fresh
        # admin retry must let the sweep pick them up again.
        .values(
            embedding_status="pending",
            embedding_error=None,
            embedding_retry_count=0,
            embedding_erasure_restored_at=case(
                (Memory.embedding_suppressed_at.is_not(None), func.clock_timestamp()),
                else_=Memory.embedding_erasure_restored_at,
            ),
            embedding_suppressed_at=None,
        )
    )
    result = cast(CursorResult[Any], await db.execute(stmt))
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
# Context Restore from soft-deleted rows (Issue #1804)
# ============================================================================


class ContextRestoreRequest(BaseModel):
    """Restore a soft-deleted context from its rows."""

    dry_run: bool = Field(True, description="Count only (default); false restores.")
    new_name: str | None = Field(
        None,
        description="Restore under this name when a live context has taken the old one.",
    )


class ContextRestoreResponse(BaseModel):
    """What a context restore did, or would do on a dry run."""

    context_id: str
    workspace_id: str
    name: str
    renamed_from: str | None
    deleted_at: str
    deleted_by: str | None
    memories_restored: int
    memories_left_deleted: int
    files_restored: int = Field(
        0, description="Files the deletion purged that come back with the context (#1960)"
    )
    file_bytes_restored: int = Field(0, description="Their bytes, re-added to the storage usage")
    warnings: list[str]
    dry_run: bool


@router.post("/contexts/{context_id}/restore", response_model=ContextRestoreResponse)
async def restore_context(
    context_id: UUID,
    request_body: ContextRestoreRequest,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ContextRestoreResponse:
    """Restore a soft-deleted context and the memories its deletion tombstoned.

    #1804: clears ``deleted_at`` on the context and on the memories deleted
    with it, and marks those memories ``pending`` so the embedding sweep
    rebuilds their vectors (about 2,400 an hour; until then recall does not
    find them). Memories forgotten before the deletion,
    Sleep tombstones and rows the tombstone purge already removed stay gone,
    as do the context's neural edges, members' ``allowed_context_ids``
    entries and revoked resource tokens.

    Default is dry_run=True. 404 for a context with no row (see
    ``/contexts/recover``), 409 for one that is not deleted, is in a deleted
    workspace, or whose name or resource a live context now uses.
    """
    from services.context_restore import restore_deleted_context

    result = await restore_deleted_context(
        db,
        context_id,
        dry_run=request_body.dry_run,
        new_name=request_body.new_name,
        actor_id=get_user_id(user),
        actor_email=user.get("email"),
    )
    return ContextRestoreResponse(
        context_id=result.context_id,
        workspace_id=result.workspace_id,
        name=result.name,
        renamed_from=result.renamed_from,
        deleted_at=to_utc_iso(result.deleted_at) or "",
        deleted_by=result.deleted_by,
        memories_restored=result.memories_restored,
        memories_left_deleted=result.memories_left_deleted,
        files_restored=result.files_restored,
        file_bytes_restored=result.file_bytes_restored,
        warnings=result.warnings,
        dry_run=result.dry_run,
    )


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


def _same_uuid(value: object, uuid_text: str) -> bool:
    """Whether ``value`` spells the UUID ``uuid_text`` (any case or hyphenation)."""
    try:
        return UUID(str(value)) == UUID(uuid_text)
    except ValueError:
        return False


@router.post("/contexts/recover", response_model=ContextRecoveryResponse)
async def recover_context(
    request_body: ContextRecoveryRequest,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ContextRecoveryResponse:
    """Recover a deleted context from surviving Qdrant data.

    Issue #86: Admin endpoint to reconstruct context and memory records
    from Qdrant point payloads when a context has been accidentally deleted.

    #1798: deleting a context now removes its points, and the orphan sweep
    removes points left by earlier deletions, so this finds nothing for a
    context deleted on v0.88.0 or later. Its soft-deleted rows stay in
    Postgres until the tombstone purge; ``POST /contexts/{context_id}/restore``
    (#1804) brings it back from them.

    Default is dry_run=True — shows what would be recovered without making changes.
    """
    import re
    from uuid import UUID as PyUUID

    from qdrant_client.models import FieldCondition, MatchValue

    from config.settings import get_settings
    from db.qdrant import (
        KAGURA_MEMORIES_COLLECTION,
        get_qdrant_client,
        summary_points_filter,
    )
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
            # #2015: a content chunk point is part of a memory, not a memory —
            # recovering it would create a row from a fragment of the body.
            scroll_filter=summary_points_filter(
                FieldCondition(key="context_id", match=MatchValue(value=context_id))
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
        no_points = "No Qdrant points found for this context_id"
        # #1804: a context deleted on v0.88.0 or later has no points left, but
        # its rows do; say where to go instead of leaving the admin at a dead end.
        deleted_row = await db.execute(
            select(Context.id).where(
                Context.id == PyUUID(context_id), Context.deleted_at.is_not(None)
            )
        )
        if deleted_row.first() is not None:
            no_points += (
                f". The context's soft-deleted rows are still in Postgres: restore it "
                f"with POST /api/v1/admin/contexts/{context_id}/restore"
            )
        return ContextRecoveryResponse(
            context_id=context_id,
            workspace_id=request_body.workspace_id or "",
            qdrant_points_found=0,
            memories_recovered=0,
            memories_already_existed=0,
            context_record_created=False,
            search_config_restored=False,
            dry_run=request_body.dry_run,
            errors=[no_points],
        )

    # Step 2: Determine workspace_id. #1963: a context row that still exists
    # decides it — a memory must live in its context's workspace
    # (fk_memories_workspace_context) — and a request naming another one is
    # refused. Without a row: the request's, else the first point's.
    context_workspace_id = (
        await db.execute(select(Context.workspace_id).where(Context.id == PyUUID(context_id)))
    ).scalar_one_or_none()
    context_exists = context_workspace_id is not None
    first_payload = all_points[0].payload or {}
    workspace_id = request_body.workspace_id or (
        str(context_workspace_id) if context_exists else first_payload.get("workspace_id", "")
    )
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

    if context_exists and workspace_id != str(context_workspace_id):
        raise BadRequestError(
            "workspace_id does not match the context's workspace", error_code="REQ-103"
        )

    from services.context_restore import lock_recovery_authors, recovered_erasure_overrides

    authors = {
        p.payload["user_id"]
        for p in all_points
        if p.payload and isinstance(p.payload.get("user_id"), str)
    }
    point_payloads = [(PyUUID(str(p.id)), p.payload or {}) for p in all_points]
    if request_body.dry_run:
        # Apply the same erasure/cohort policy without row locks or writes.
        live_authors = await lock_recovery_authors(db, authors, lock=False)
        erasure_overrides = await recovered_erasure_overrides(db, point_payloads, live_authors)

        # Batch-check existing memory records (avoid N+1)
        point_ids = [PyUUID(str(p.id)) for p in all_points]
        existing_result = await db.execute(select(Memory.id).where(Memory.id.in_(point_ids)))
        existing_mem_ids = {row[0] for row in existing_result.all()}

        # Check if search config exists independently of context
        existing_cfg = await db.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == PyUUID(context_id))
        )
        config_exists = existing_cfg.scalar_one_or_none() is not None
        skipped = {
            memory_id
            for memory_id, policy in erasure_overrides.items()
            if policy is None and memory_id not in existing_mem_ids
        }
        errors.extend(
            f"Skipped memory {memory_id}: cannot verify its post-erasure author cohort"
            for memory_id in sorted(skipped)
        )

        return ContextRecoveryResponse(
            context_id=context_id,
            workspace_id=workspace_id,
            qdrant_points_found=len(all_points),
            memories_recovered=len(all_points) - len(existing_mem_ids) - len(skipped),
            memories_already_existed=len(existing_mem_ids),
            context_record_created=not context_exists,
            search_config_restored=not config_exists,
            dry_run=True,
            errors=errors,
        )

    # #1798: the rows created below belong to points that already exist. Until
    # this transaction commits they look row-less to the orphan sweep.
    from db.point_writer_lock import hold_point_writer_lock

    # Users precede the global writer and context/memory locks. Recovery must
    # serialize with erasure before trusting a raw author from an old snapshot.
    live_authors = await lock_recovery_authors(db, authors)
    await hold_point_writer_lock(db)
    erasure_overrides = await recovered_erasure_overrides(db, point_payloads, live_authors)

    # Step 3: Create Context record if missing. Read again just before the
    # writes (the lock above is shared and only keeps the orphan sweep out):
    # a row hard-deleted since step 2 is recreated, and a row another
    # recovery created meanwhile in another workspace is refused like any
    # mismatch, rather than failing the inserts on the foreign keys (#1963).
    context_record_created = False
    context_workspace_id = (
        await db.execute(select(Context.workspace_id).where(Context.id == PyUUID(context_id)))
    ).scalar_one_or_none()
    context_exists = context_workspace_id is not None
    if context_exists and str(context_workspace_id) != workspace_id:
        raise BadRequestError(
            "workspace_id does not match the context's workspace", error_code="REQ-103"
        )
    if not context_exists:
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
            # #1207: the lost row's setting is unknowable from Qdrant, so
            # recovery pins the non-ranking-modifying value rather than the
            # new-context default (recall ordering must not change as a side
            # effect of disaster recovery). Re-enable via update_search_config.
            # The reranker columns follow the same rule: ORM defaults (off),
            # deliberately not the #1572 deployment default.
            reinforce_enabled=False,
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
        if mem_id in erasure_overrides and erasure_overrides[mem_id] is None:
            errors.append(f"Skipped memory {mem_id}: cannot verify its post-erasure author cohort")
            continue
        # #1963: a point whose payload names another workspace (or none) is
        # missed by the workspace-filtered search, so the row goes back
        # through the embedding sweep, which rewrites the point's payload.
        point_matches = _same_uuid(payload.get("workspace_id"), workspace_id)
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
                embedding_status="success" if point_matches else "pending",
                client="admin-recovery",
                source="admin_recovery",
            )
            for field, value in (erasure_overrides.get(mem_id) or {}).items():
                setattr(new_memory, field, value)
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
