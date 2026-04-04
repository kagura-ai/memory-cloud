"""Admin Plan Management API Routes.

Issue #149: Plan tier enforcement - Admin management endpoints
Issue #252: Session-only authentication (no API keys)

Allows admins to:
- View all workspaces with plan info
- Change workspace plan tiers
- Set custom quota overrides
- View plan change audit log
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_admin as auth_require_admin
from config.plan_tiers import get_plan_tier
from db.base import get_db
from models.auth import Context, PlanChange, User, Workspace, WorkspaceInvitation, WorkspaceMember
from models.memory import Memory
from services.effective_quota_service import EffectiveQuotaService
from utils import db_transaction
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/plans", tags=["admin-plans"])


# ============================================================================
# Request/Response Models
# ============================================================================


class WorkspacePlanInfo(BaseModel):
    """Workspace with plan information.

    Issue #276: Slug removed.
    """

    id: str
    name: str
    plan_name: str
    owner_user_id: str
    owner_name: str | None = None
    owner_email: str | None = None
    total_memories: int
    memory_limit: int
    daily_api_limit: int
    weekly_api_limit: int


class UpdatePlanRequest(BaseModel):
    """Request to update workspace plan tier."""

    plan_name: str = Field(..., pattern=r"^(free|basic|pro)$")
    reason: str | None = None


class QuotaBreakdown(BaseModel):
    """Quota values for a single tier."""

    memory_limit: int
    mcp_calls_per_day: int
    max_contexts: int
    max_members: int


class AddonValues(BaseModel):
    """Addon bonus values."""

    memory_bonus: int = 0
    mcp_quota_bonus: int = 0
    member_bonus: int = 0
    context_bonus: int = 0


class UsageValues(BaseModel):
    """Current usage values."""

    memories: int
    contexts: int
    members: int


class WorkspaceQuotaDetail(BaseModel):
    """Detailed quota breakdown for a workspace."""

    workspace_id: str
    workspace_name: str
    plan_name: str
    base: QuotaBreakdown
    addon: AddonValues
    effective: QuotaBreakdown
    usage: UsageValues


class UpdateAddonRequest(BaseModel):
    """Request to update workspace addon bonuses."""

    addon_memory_bonus: int = Field(0, ge=0)
    addon_mcp_quota_bonus: int = Field(0, ge=0)
    addon_member_bonus: int = Field(0, ge=0)
    addon_context_bonus: int = Field(0, ge=0)


class PlanChangeAuditEntry(BaseModel):
    """Plan change audit log entry."""

    id: int
    workspace_id: str
    workspace_name: str
    old_plan: str | None
    new_plan: str
    changed_by: str
    changed_at: str
    reason: str | None


# ============================================================================
# Admin-only Dependency
# ============================================================================

# Issue #252: Use existing require_admin from auth.dependencies (Session-only)
require_admin = auth_require_admin


# ============================================================================
# Admin Plan Management Endpoints
# ============================================================================


@router.get("/workspaces", response_model=list[WorkspacePlanInfo])
async def list_workspaces_with_plans(
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all workspaces with plan info and usage stats.

    Admin-only endpoint - shows all workspaces in the system.

    Returns:
        List of all workspaces with plan tiers and current usage
    """
    async with db_transaction(db, "list_workspaces_with_plans", "Failed to list workspaces"):
        # Get ALL workspaces (admin can see all)
        workspaces_result = await db.execute(
            select(Workspace).where(Workspace.deleted_at.is_(None)).order_by(Workspace.created_at)
        )
        workspaces = workspaces_result.scalars().all()

        # Get owner info for all workspaces
        from models.auth import User

        owner_ids = [workspace.owner_user_id for workspace in workspaces]
        if owner_ids:
            users_result = await db.execute(select(User).where(User.user_id.in_(owner_ids)))
            users = {u.user_id: u for u in users_result.scalars().all()}
        else:
            users = {}

        workspace_infos = []

        # Critical Fix: Avoid N+1 query - fetch all workspace members and memories in bulk
        workspace_ids = [w.id for w in workspaces]

        # Fetch all members for all workspaces in one query
        all_members_result = await db.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(workspace_ids))
        )
        members_by_workspace = {}
        for member in all_members_result.scalars():
            if member.workspace_id not in members_by_workspace:
                members_by_workspace[member.workspace_id] = []
            members_by_workspace[member.workspace_id].append(member.user_id)

        # Fetch all memory counts grouped by workspace
        memory_counts_result = await db.execute(
            select(Memory.workspace_id, func.count(Memory.id).label("memory_count"))
            .where(
                Memory.workspace_id.in_(workspace_ids),
                Memory.deleted_at.is_(None),
            )
            .group_by(Memory.workspace_id)
        )
        memory_counts_by_workspace = {
            row.workspace_id: row.memory_count for row in memory_counts_result.all()
        }

        for workspace in workspaces:
            total_memories = memory_counts_by_workspace.get(workspace.id, 0)

            owner = users.get(workspace.owner_user_id)
            workspace_infos.append(
                WorkspacePlanInfo(
                    id=str(workspace.id),
                    name=workspace.name,
                    plan_name=workspace.plan_name,
                    owner_user_id=workspace.owner_user_id,
                    owner_name=owner.name if owner else None,
                    owner_email=owner.email if owner else None,
                    total_memories=total_memories,
                    memory_limit=workspace.effective_memory_limit,
                    daily_api_limit=workspace.effective_daily_api_limit,
                    weekly_api_limit=workspace.effective_weekly_api_limit,
                )
            )

        logger.info(
            f"Admin listed {len(workspace_infos)} workspaces", admin_user=admin_user["user_id"]
        )
        return workspace_infos


@router.put("/workspaces/{workspace_id}/plan")
async def update_workspace_plan(
    workspace_id: str,
    request: UpdatePlanRequest,
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Change workspace plan tier.

    Admin-only endpoint.

    Args:
        workspace_id: Workspace ID
        request: Update plan request with new plan name and optional reason

    Returns:
        Success message

    Raises:
        404: Workspace not found
        422: Invalid plan tier
    """
    from uuid import UUID

    async with db_transaction(db, "update_workspace_plan", "Failed to update workspace plan"):
        # Get workspace
        workspace_result = await db.execute(
            select(Workspace).where(Workspace.id == UUID(workspace_id))
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            raise HTTPException(status_code=404, detail=f"Workspace {workspace_id} not found")

        # Validate new plan tier
        try:
            new_plan_tier = get_plan_tier(request.plan_name)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

        # Store old values for audit
        old_plan = workspace.plan_name
        old_memory_limit = workspace.memory_limit
        old_daily_limit = workspace.daily_api_limit
        old_weekly_limit = workspace.weekly_api_limit

        # Update workspace plan and quotas
        workspace.plan_name = request.plan_name
        workspace.memory_limit = new_plan_tier.memory_limit
        workspace.daily_api_limit = new_plan_tier.daily_api_limit
        workspace.weekly_api_limit = new_plan_tier.weekly_api_limit

        # Create audit log entry
        audit_entry = PlanChange(
            workspace_id=workspace.id,
            old_plan=old_plan,
            new_plan=request.plan_name,
            changed_by=admin_user["user_id"],
            changed_at=utcnow(),
            reason=request.reason,
            old_memory_limit=old_memory_limit,
            old_daily_api_limit=old_daily_limit,
            old_weekly_api_limit=old_weekly_limit,
            new_memory_limit=new_plan_tier.memory_limit,
            new_daily_api_limit=new_plan_tier.daily_api_limit,
            new_weekly_api_limit=new_plan_tier.weekly_api_limit,
        )
        db.add(audit_entry)

        # Issue #149: If downgrading to Free, disable reranking on all contexts
        if request.plan_name == "free" and old_plan in ("basic", "pro"):
            from models.auth import Context
            from models.config import ContextSearchConfig

            # Get all contexts for this workspace
            contexts_result = await db.execute(
                select(Context).where(
                    Context.workspace_id == UUID(workspace_id), Context.deleted_at.is_(None)
                )
            )
            contexts_list = contexts_result.scalars().all()

            # Critical Fix: Avoid N+1 query - batch fetch all context configs
            context_ids = [ctx.id for ctx in contexts_list]
            configs_result = await db.execute(
                select(ContextSearchConfig).where(ContextSearchConfig.context_id.in_(context_ids))
            )
            configs_by_context = {cfg.context_id: cfg for cfg in configs_result.scalars()}

            # Disable reranking on all context configs
            disabled_count = 0
            for context in contexts_list:
                config = configs_by_context.get(context.id)

                if config and config.use_rerank:
                    config.use_rerank = False
                    disabled_count += 1
                    logger.info(
                        f"Disabled reranking for context {context.id} "
                        f"due to workspace downgrade to Free plan"
                    )

            if disabled_count > 0:
                logger.info(
                    f"Disabled reranking on {disabled_count} context(s) "
                    f"for workspace {workspace_id} (downgrade to Free)"
                )

        await db.commit()

        logger.info(
            "admin_plan_changed",
            admin_user=admin_user["user_id"],
            workspace_id=workspace_id,
            old_plan=old_plan,
            new_plan=request.plan_name,
            reason=request.reason,
        )

        return {"message": f"Plan changed from {old_plan} to {request.plan_name}"}


@router.get("/audit", response_model=list[PlanChangeAuditEntry])
async def get_plan_change_audit(
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    """Get plan change audit log.

    Admin-only endpoint.

    Args:
        limit: Maximum entries to return (default: 100)

    Returns:
        List of plan change audit entries
    """
    async with db_transaction(db, "get_plan_audit", "Failed to get audit log"):
        # Get audit entries with workspace names and user names
        audit_result = await db.execute(
            select(PlanChange, Workspace.name, User.name)
            .join(Workspace, PlanChange.workspace_id == Workspace.id)
            .outerjoin(User, PlanChange.changed_by == User.user_id)
            .order_by(PlanChange.changed_at.desc())
            .limit(limit)
        )

        entries = []
        for audit, workspace_name, user_name in audit_result.all():
            entries.append(
                PlanChangeAuditEntry(
                    id=audit.id,
                    workspace_id=str(audit.workspace_id),
                    workspace_name=workspace_name,
                    old_plan=audit.old_plan,
                    new_plan=audit.new_plan,
                    changed_by=user_name or audit.changed_by,
                    changed_at=audit.changed_at.isoformat(),
                    reason=audit.reason,
                )
            )

        logger.info(
            f"Admin retrieved {len(entries)} audit entries", admin_user=admin_user["user_id"]
        )
        return entries


# ============================================================================
# Workspace Quota Detail Endpoints (Issue #325)
# ============================================================================


@router.get("/workspaces/{workspace_id}/quotas", response_model=WorkspaceQuotaDetail)
async def get_workspace_quotas(
    workspace_id: str,
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed quota breakdown for a workspace.

    Returns base plan limits, addon bonuses, effective totals, and current usage.
    """
    ws_uuid = UUID(workspace_id)

    async with db_transaction(db, "get_workspace_quotas", "Failed to get quota details"):
        # Get workspace
        result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
        workspace = result.scalar_one_or_none()
        if not workspace:
            raise HTTPException(status_code=404, detail="Workspace not found")

        plan_tier = get_plan_tier(workspace.plan_name)

        # Get effective quotas
        effective_service = EffectiveQuotaService(db)
        effective = await effective_service.get_effective_quotas(ws_uuid)

        # Get usage: memories count
        memory_count_result = await db.execute(
            select(func.count(Memory.id)).where(
                Memory.workspace_id == ws_uuid,
                Memory.deleted_at.is_(None),
            )
        )
        memory_count = memory_count_result.scalar() or 0

        # Get usage: context count (non-deleted only)
        context_count_result = await db.execute(
            select(func.count(Context.id)).where(
                Context.workspace_id == ws_uuid,
                Context.deleted_at.is_(None),
            )
        )
        context_count = context_count_result.scalar() or 0

        # Get usage: member count + pending invitations
        member_count_result = await db.execute(
            select(func.count(WorkspaceMember.id)).where(WorkspaceMember.workspace_id == ws_uuid)
        )
        pending_invite_result = await db.execute(
            select(func.count(WorkspaceInvitation.id)).where(
                WorkspaceInvitation.workspace_id == ws_uuid,
                WorkspaceInvitation.accepted_at.is_(None),
                WorkspaceInvitation.expires_at > func.now(),
            )
        )
        member_count = (member_count_result.scalar() or 0) + (pending_invite_result.scalar() or 0)

        return WorkspaceQuotaDetail(
            workspace_id=workspace_id,
            workspace_name=workspace.name,
            plan_name=workspace.plan_name,
            base=QuotaBreakdown(
                memory_limit=workspace.memory_limit,
                mcp_calls_per_day=plan_tier.mcp_calls_per_day,
                max_contexts=plan_tier.max_contexts_per_workspace,
                max_members=plan_tier.max_members_per_workspace,
            ),
            addon=AddonValues(
                memory_bonus=workspace.addon_memory_bonus,
                mcp_quota_bonus=workspace.addon_mcp_quota_bonus,
                member_bonus=workspace.addon_member_bonus,
                context_bonus=workspace.addon_context_bonus,
            ),
            effective=QuotaBreakdown(
                memory_limit=effective["memory_limit"],
                mcp_calls_per_day=effective["mcp_calls_per_day"],
                max_contexts=effective["max_contexts"],
                max_members=effective["max_members"],
            ),
            usage=UsageValues(
                memories=memory_count,
                contexts=context_count,
                members=member_count,
            ),
        )


@router.put("/workspaces/{workspace_id}/quotas")
async def update_workspace_quotas(
    workspace_id: str,
    request: UpdateAddonRequest,
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update workspace addon bonuses.

    Validates that reducing addons won't put usage over effective limits.
    """
    ws_uuid = UUID(workspace_id)

    async with db_transaction(db, "update_workspace_quotas", "Failed to update quotas"):
        # Get workspace
        result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
        workspace = result.scalar_one_or_none()
        if not workspace:
            raise HTTPException(status_code=404, detail="Workspace not found")

        plan_tier = get_plan_tier(workspace.plan_name)

        # Calculate new effective values
        new_effective_members = plan_tier.max_members_per_workspace + request.addon_member_bonus
        new_effective_contexts = plan_tier.max_contexts_per_workspace + request.addon_context_bonus

        # Hard limit: members — cannot reduce below current count + pending invitations
        member_count_result = await db.execute(
            select(func.count(WorkspaceMember.id)).where(WorkspaceMember.workspace_id == ws_uuid)
        )
        pending_invite_result = await db.execute(
            select(func.count(WorkspaceInvitation.id)).where(
                WorkspaceInvitation.workspace_id == ws_uuid,
                WorkspaceInvitation.accepted_at.is_(None),
                WorkspaceInvitation.expires_at > func.now(),
            )
        )
        member_count = (member_count_result.scalar() or 0) + (pending_invite_result.scalar() or 0)

        if member_count > new_effective_members:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reduce member quota: current member count ({member_count}) exceeds new effective limit ({new_effective_members})",
            )

        # Hard limit: contexts — cannot reduce below current count
        context_count_result = await db.execute(
            select(func.count(Context.id)).where(
                Context.workspace_id == ws_uuid,
                Context.deleted_at.is_(None),
            )
        )
        context_count = context_count_result.scalar() or 0

        if context_count > new_effective_contexts:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reduce context quota: current context count ({context_count}) exceeds new effective limit ({new_effective_contexts})",
            )

        # Store old values for logging
        old_memory_bonus = workspace.addon_memory_bonus
        old_mcp_bonus = workspace.addon_mcp_quota_bonus
        old_member_bonus = workspace.addon_member_bonus
        old_context_bonus = workspace.addon_context_bonus

        # Update addon bonuses
        workspace.addon_memory_bonus = request.addon_memory_bonus
        workspace.addon_mcp_quota_bonus = request.addon_mcp_quota_bonus
        workspace.addon_member_bonus = request.addon_member_bonus
        workspace.addon_context_bonus = request.addon_context_bonus

        await db.commit()

        logger.info(
            "workspace_addon_updated",
            workspace_id=workspace_id,
            admin_user=admin_user["user_id"],
            changes={
                "memory_bonus": f"{old_memory_bonus} -> {request.addon_memory_bonus}",
                "mcp_quota_bonus": f"{old_mcp_bonus} -> {request.addon_mcp_quota_bonus}",
                "member_bonus": f"{old_member_bonus} -> {request.addon_member_bonus}",
                "context_bonus": f"{old_context_bonus} -> {request.addon_context_bonus}",
            },
        )

        return {"message": f"Quota addons updated for workspace {workspace.name}"}
