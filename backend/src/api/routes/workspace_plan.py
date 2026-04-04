"""Workspace Plan Management API Routes.

Issue #164: User Management拡張 + Plan Management改善
Issue #252: Session-only authentication (no API keys)

Allows workspace owners/admins to manage their workspace's plan.
This is a non-admin alternative to /admin/plans endpoints.

Self-service plan changes (PUT) require BILLING_ENABLED=true.
When billing is disabled, plan changes are admin-only via /admin/plans endpoints.

Endpoints:
- GET /api/v1/workspaces/{workspace_id}/plan - Get workspace plan info
- PUT /api/v1/workspaces/{workspace_id}/plan - Change plan (owner only, requires billing)
- GET /api/v1/workspaces/plans/available - List available plans
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from config.plan_tiers import PLAN_TIERS, PlanName, get_plan_tier
from db.base import get_db
from models.auth import Context, PlanChange, Workspace, WorkspaceMember
from models.config import ContextSearchConfig
from models.memory import Memory
from services.member_credentials_service import MemberCredentialsService
from services.permission_service import PermissionService
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["workspace-plan"])


# ============================================================================
# Request/Response Models
# ============================================================================


class WorkspacePlanInfo(BaseModel):
    """Workspace plan information with usage stats."""

    workspace_id: str
    workspace_name: str
    current_plan: str
    plan_display_name: str
    price_monthly: int
    usage: dict  # Current usage (memories, storage, contexts)
    quotas: dict  # Plan quotas
    can_upgrade: bool
    can_downgrade: bool


class AvailablePlanInfo(BaseModel):
    """Available plan tier information."""

    name: str
    display_name: str
    price_monthly: int
    quotas: dict
    features: list[str]


class UpdatePlanRequest(BaseModel):
    """Request to update workspace plan."""

    plan_name: str = Field(..., description="New plan tier (free/basic/pro)")
    reason: str | None = Field(None, description="Optional reason for change")


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/workspaces/{workspace_id}/plan", response_model=WorkspacePlanInfo)
async def get_workspace_plan(
    workspace_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Get workspace plan information.

    Issue #246: Owner-only access (frontend shows in owner-only menu).

    Returns current plan, usage stats, quotas, and upgrade/downgrade options.
    """
    workspace_uuid = UUID(workspace_id)

    # Issue #246: Owner-only access
    perm_service = PermissionService(db)
    await perm_service.check_workspace_owner(user["user_id"], workspace_uuid)

    # Get workspace
    workspace_result = await db.execute(select(Workspace).where(Workspace.id == workspace_uuid))
    workspace = workspace_result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Get plan tier info
    plan_tier = get_plan_tier(workspace.plan_name)

    # Get current usage stats
    # Count memories across all workspace members
    members_result = await db.execute(
        select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == workspace_uuid)
    )
    member_user_ids = [row[0] for row in members_result.all()]

    memory_count = 0
    if member_user_ids:
        memory_count_result = await db.execute(
            select(func.count(Memory.id)).where(Memory.user_id.in_(member_user_ids))
        )
        memory_count = memory_count_result.scalar() or 0

    # Count contexts
    context_count_result = await db.execute(
        select(func.count(Context.id)).where(
            Context.workspace_id == workspace_uuid, Context.deleted_at.is_(None)
        )
    )
    context_count = context_count_result.scalar() or 0

    # Log for debugging
    logger.info(
        "get_workspace_plan",
        workspace_id=str(workspace_uuid),
        workspace_name=workspace.name,
        plan_name=workspace.plan_name,
        context_count=context_count,
        max_contexts=plan_tier.max_contexts_per_workspace,
        memory_count=memory_count,
    )

    # Determine upgrade/downgrade options
    plan_order = ["free", "basic", "pro"]
    current_index = (
        plan_order.index(workspace.plan_name) if workspace.plan_name in plan_order else 0
    )

    return WorkspacePlanInfo(
        workspace_id=str(workspace.id),
        workspace_name=workspace.name,
        current_plan=workspace.plan_name,
        plan_display_name=plan_tier.display_name,
        price_monthly=plan_tier.price_monthly,
        usage={
            "memories": memory_count,
            "contexts": context_count,
        },
        quotas={
            "memory_limit": workspace.effective_memory_limit,
            "max_contexts": workspace.effective_max_contexts,
            "max_resource_tokens": plan_tier.max_resource_tokens,  # Issue #242
            "max_quota_capacity": plan_tier.max_resource_tokens * 10000,  # Issue #242: events/hour
            "daily_api_limit": workspace.effective_mcp_calls_per_day,
            "weekly_api_limit": workspace.effective_mcp_calls_per_day * 7,
        },
        can_upgrade=current_index < len(plan_order) - 1,
        can_downgrade=current_index > 0,
    )


@router.put("/workspaces/{workspace_id}/plan")
async def update_workspace_plan(
    workspace_id: str,
    request: UpdatePlanRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Change workspace plan tier.

    Accessible by: Workspace OWNER ONLY
    Requires: BILLING_ENABLED=true (otherwise admin-only via /admin/plans)

    Updates plan_name and quota limits based on plan tier.
    Creates audit log entry for the change.
    """
    # Billing guard: self-service plan changes require billing plugin
    from plugins.billing import is_billing_enabled

    if not is_billing_enabled():
        raise HTTPException(
            status_code=403,
            detail="Self-service plan changes are disabled. "
            "Contact your system administrator to change your plan.",
        )

    workspace_uuid = UUID(workspace_id)

    # Check owner permission (admin cannot change plan)
    perm_service = PermissionService(db)
    await perm_service.check_workspace_owner(user["user_id"], workspace_uuid)

    # Get workspace
    workspace_result = await db.execute(select(Workspace).where(Workspace.id == workspace_uuid))
    workspace = workspace_result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Validate plan
    if request.plan_name not in PLAN_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid plan: {request.plan_name}. Valid plans: {list(PLAN_TIERS.keys())}",
        )

    old_plan = workspace.plan_name
    if old_plan == request.plan_name:
        raise HTTPException(status_code=400, detail="Workspace is already on this plan")

    # Get new plan tier
    new_tier = PLAN_TIERS[request.plan_name]

    # Save old values for audit
    old_memory_limit = workspace.memory_limit
    old_daily_api = workspace.daily_api_limit
    old_weekly_api = workspace.weekly_api_limit

    # Issue #196: Remove members BEFORE updating plan (so old_plan is still "pro")
    members_removed = 0
    credentials_cleaned = {}

    if old_plan == PlanName.PRO and request.plan_name in [PlanName.FREE, PlanName.BASIC]:
        # Get all members except owner
        members_result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_uuid,
                WorkspaceMember.role != "owner",
            )
        )
        members_to_remove = members_result.scalars().all()

        # Issue #275 High: Optimize by getting owner once (instead of N times)
        # Get workspace owner for ownership transfers
        owner_result = await db.execute(
            select(WorkspaceMember.user_id).where(
                and_(
                    WorkspaceMember.workspace_id == workspace_uuid,
                    WorkspaceMember.role == "owner",
                )
            )
        )
        owner_id = owner_result.scalar_one()

        # Remove members and cleanup credentials
        cred_service = MemberCredentialsService(db)

        for member in members_to_remove:
            # Issue #275 Critical: Comprehensive cleanup with ownership transfers
            # Issue #275 High: Pass owner_id to avoid N queries
            context_cleanup = await cred_service.cleanup_context_members(
                workspace_uuid, member.user_id
            )
            cred_cleanup = await cred_service.cleanup_member_credentials(
                workspace_uuid, member.user_id
            )
            memory_transfer = await cred_service.cleanup_member_memories(
                workspace_uuid, member.user_id, owner_id
            )
            context_transfer = await cred_service.cleanup_member_contexts(
                workspace_uuid, member.user_id, owner_id
            )
            resource_token_transfer = await cred_service.cleanup_member_resource_tokens(
                workspace_uuid, member.user_id, owner_id
            )
            invitation_cleanup = await cred_service.cleanup_member_invitations(
                workspace_uuid, member.user_id
            )

            # Aggregate cleanup stats
            for key, value in cred_cleanup.items():
                credentials_cleaned[key] = credentials_cleaned.get(key, 0) + value

            credentials_cleaned["context_members_deleted"] = (
                credentials_cleaned.get("context_members_deleted", 0)
                + context_cleanup["context_members_deleted"]
            )
            credentials_cleaned["memories_transferred"] = (
                credentials_cleaned.get("memories_transferred", 0)
                + memory_transfer["memories_transferred"]
            )
            credentials_cleaned["contexts_transferred"] = (
                credentials_cleaned.get("contexts_transferred", 0)
                + context_transfer["contexts_transferred"]
            )
            credentials_cleaned["resource_tokens_transferred"] = (
                credentials_cleaned.get("resource_tokens_transferred", 0)
                + resource_token_transfer["resource_tokens_transferred"]
            )
            credentials_cleaned["invitations_deleted"] = (
                credentials_cleaned.get("invitations_deleted", 0)
                + invitation_cleanup["invitations_deleted"]
            )

            # Remove member
            await db.delete(member)
            members_removed += 1

        await db.commit()

        logger.info(
            "members_removed_on_plan_downgrade",
            workspace_id=str(workspace_uuid),
            members_removed=members_removed,
            credentials_cleaned=credentials_cleaned,
        )

    # Update workspace
    workspace.plan_name = request.plan_name
    workspace.memory_limit = new_tier.memory_limit
    workspace.daily_api_limit = new_tier.daily_api_limit
    workspace.weekly_api_limit = new_tier.weekly_api_limit

    await db.commit()

    # Create audit log
    plan_change = PlanChange(
        workspace_id=workspace_uuid,
        old_plan=old_plan,
        new_plan=request.plan_name,
        changed_by=user["user_id"],
        reason=request.reason,
        old_memory_limit=old_memory_limit,
        old_daily_api_limit=old_daily_api,
        old_weekly_api_limit=old_weekly_api,
        new_memory_limit=new_tier.memory_limit,
        new_daily_api_limit=new_tier.daily_api_limit,
        new_weekly_api_limit=new_tier.weekly_api_limit,
    )
    db.add(plan_change)

    # If downgrading from Pro: Handle public contexts and resource tokens (Issue #242)
    # NOTE: All operations in single transaction (no intermediate commits)
    resource_tokens_revoked = 0

    if old_plan == PlanName.PRO and request.plan_name in [PlanName.FREE, PlanName.BASIC]:
        # Revoke all active resource tokens (Public contexts feature is PRO only)
        from auth.resource_tokens import ResourceTokenManager
        from models.resource import ResourceToken

        token_manager = ResourceTokenManager(db)

        # Get all active tokens created by workspace members
        members_result = await db.execute(
            select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == workspace_uuid)
        )
        member_user_ids = [row[0] for row in members_result.all()]

        if member_user_ids:
            active_tokens_result = await db.execute(
                select(ResourceToken).where(
                    and_(
                        ResourceToken.created_by.in_(member_user_ids),
                        ResourceToken.is_active == True,  # noqa: E712
                    )
                )
            )
            active_tokens = active_tokens_result.scalars().all()

            for token in active_tokens:
                await token_manager.revoke_token(token.id)
                resource_tokens_revoked += 1

            if resource_tokens_revoked > 0:
                logger.info(
                    "resource_tokens_revoked_on_downgrade",
                    workspace_id=str(workspace_uuid),
                    count=resource_tokens_revoked,
                    reason="Downgrade to Free/Basic (Public contexts not supported)",
                )

        # Note: Public contexts settings (is_public=True, resource_id) are PRESERVED
        # They will be re-enabled when user upgrades back to Pro

    # If downgrading to Free/Basic from Pro: Handle contexts (Issue #196)
    shared_contexts_converted = 0

    if old_plan == PlanName.PRO and request.plan_name in [PlanName.FREE, PlanName.BASIC]:
        # Get max contexts for new plan
        max_contexts = new_tier.max_contexts_per_workspace  # 1 for free, 3 for basic

        # Get all active contexts
        all_contexts_result = await db.execute(
            select(Context)
            .where(Context.workspace_id == workspace_uuid, Context.deleted_at.is_(None))
            .order_by(Context.created_at.asc())
        )
        all_contexts = all_contexts_result.scalars().all()

        # Count shared contexts
        shared_context_count = sum(1 for ctx in all_contexts if not ctx.is_private)

        # Check if exceeds max_contexts limit
        if len(all_contexts) > max_contexts:
            contexts_to_delete = len(all_contexts) - max_contexts
            # Build detailed message
            message = (
                f"Cannot downgrade to {request.plan_name}. "
                f"You have {len(all_contexts)} contexts but {request.plan_name} allows only {max_contexts}. "
                f"Please delete {contexts_to_delete} context(s) before downgrading."
            )
            if shared_context_count > 0:
                message += (
                    f" Note: {shared_context_count} shared context(s) will be converted to private "
                    f"after downgrade (Free/Basic plans don't support shared contexts)."
                )
            raise HTTPException(status_code=400, detail=message)

        # Convert shared contexts to private (data preserved, visibility changed)
        for context in all_contexts:
            if not context.is_private:
                context.is_private = True
                shared_contexts_converted += 1

        # Don't commit yet - wait for all operations to complete

        if shared_contexts_converted > 0:
            logger.info(
                "shared_contexts_converted_to_private",
                workspace_id=str(workspace_uuid),
                contexts_converted=shared_contexts_converted,
            )

    # If downgrading to Free: Disable reranking on all contexts
    if request.plan_name == PlanName.FREE:
        # Get remaining contexts after cleanup
        remaining_contexts_result = await db.execute(
            select(Context.id).where(
                Context.workspace_id == workspace_uuid, Context.deleted_at.is_(None)
            )
        )
        remaining_context_ids = [row[0] for row in remaining_contexts_result.all()]

        if remaining_context_ids:
            # Disable reranking
            await db.execute(
                update(ContextSearchConfig)
                .where(ContextSearchConfig.context_id.in_(remaining_context_ids))
                .values(use_rerank=False)
            )
            await db.commit()

            logger.info(
                "reranking_disabled_for_free_plan",
                workspace_id=str(workspace_uuid),
                context_count=len(remaining_context_ids),
            )

    logger.info(
        "workspace_plan_changed",
        workspace_id=str(workspace_uuid),
        workspace_name=workspace.name,
        old_plan=old_plan,
        new_plan=request.plan_name,
        changed_by=user["user_id"],
        members_removed=members_removed,
    )

    # Return with cleanup summary
    response = {
        "message": f"Plan changed from {old_plan} to {request.plan_name}",
        "members_removed": members_removed,
        "shared_contexts_converted": shared_contexts_converted,
        "resource_tokens_revoked": resource_tokens_revoked,
    }

    if credentials_cleaned:
        response["credentials_cleaned"] = credentials_cleaned

    if resource_tokens_revoked > 0:
        response["notice"] = (
            f"⚠️ IMPORTANT: {resource_tokens_revoked} resource token(s) have been revoked. "
            "External systems using these tokens will receive 401 errors. "
            "Public context settings are preserved and will be re-enabled when you upgrade to Pro. "
            "Please notify affected API consumers."
        )
        # TODO (Code review C-1): Implement email/webhook notifications for token revocation
        # Track affected tokens and send notifications to integration owners

    return response


@router.get("/workspaces/plans/available", response_model=list[AvailablePlanInfo])
async def get_available_plans(
    user: SessionUser,
):
    """Get available plan tiers.

    Public information, accessible by any authenticated user.
    """
    # Feature display name mapping
    feature_display_names = {
        "api_keys": "API Keys",
        "reranking": "Search Reranking",
        "oauth": "OAuth Applications",
        "memory_agent": "Memory Agent",
        "team_invitations": "Team Invitations",
        "shared_contexts": "Shared Contexts",
    }

    return [
        AvailablePlanInfo(
            name=tier.name,
            display_name=tier.display_name,
            price_monthly=tier.price_monthly,
            quotas={
                "memory_limit": tier.memory_limit,
                "max_contexts": tier.max_contexts_per_workspace,
                "daily_api_limit": tier.daily_api_limit,
                "weekly_api_limit": tier.weekly_api_limit,
            },
            features=[feature_display_names.get(f, f) for f in sorted(tier.features)],
        )
        for tier in PLAN_TIERS.values()
    ]
