"""Quota Service for Plan Tier Enforcement.

Issue #149: Implements quota checking and feature gating for Free/Basic/Pro plans.

Responsibilities:
- Check memory quotas before creating memories
- Check feature access (reranking, OAuth, Memory Agent)
- Check multi-workspace restrictions
- Provide quota status and warnings
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import PLAN_TIERS, get_plan_tier, has_feature
from models.auth import (
    Context,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.memory import Memory
from services.effective_quota_service import EffectiveQuotaService
from utils.datetime import utcnow
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError
from utils.logger import get_logger

logger = get_logger(__name__)


class QuotaService:
    """Service for checking quotas and feature access based on plan tiers.

    Issue #149: Plan tier enforcement.
    """

    def __init__(self, db: AsyncSession):
        """Initialize quota service.

        Args:
            db: Database session
        """
        self.db = db

    # ========================================================================
    # Memory Quota Checks
    # ========================================================================

    async def check_memory_quota(
        self,
        workspace_id: UUID,
        raise_on_exceeded: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if workspace can create more memories.

        Args:
            workspace_id: Workspace ID
            raise_on_exceeded: If True, raise QuotaExceededError instead of returning False

        Returns:
            Tuple of (can_create, error_message)

        Raises:
            QuotaExceededError: If raise_on_exceeded=True and quota exceeded
        """
        # Issue #273 H-5: Add row-level locking to prevent race conditions
        # Get workspace with plan limits (with FOR UPDATE lock)
        workspace_result = await self.db.execute(
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .with_for_update()  # Lock workspace row during quota check
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            error = f"Workspace {workspace_id} not found"
            if raise_on_exceeded:
                raise QuotaExceededError(error)
            return False, error

        # Count memories across all workspace members (optimized single query with JOIN)
        # Issue #273 C-2: Add NULL workspace_id and deleted_at filters to prevent quota bypass
        # Note: This count is still subject to TOCTOU race conditions between check and insert.
        #       For strict enforcement, consider adding a database CHECK constraint.
        memory_count_result = await self.db.execute(
            select(func.count(Memory.id))
            .select_from(Memory)
            .join(WorkspaceMember, Memory.user_id == WorkspaceMember.user_id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                Memory.workspace_id.isnot(None),  # Exclude NULL workspace_id (orphaned memories)
                Memory.deleted_at.is_(None),  # Exclude soft-deleted memories
            )
        )
        current_count = memory_count_result.scalar() or 0

        # If no memories, workspace has no usage
        if current_count == 0:
            return True, None

        # Issue #238: Use effective quotas (base + addons)
        effective_quota_service = EffectiveQuotaService(self.db)
        effective_quotas = await effective_quota_service.get_effective_quotas(workspace_id)
        memory_limit = effective_quotas["memory_limit"]

        # Check against effective limit
        if current_count >= memory_limit:
            error = (
                f"Memory quota exceeded. "
                f"Current: {current_count}, Limit: {memory_limit} ({workspace.plan_name} plan + addons)"
            )
            logger.warning(
                "memory_quota_exceeded",
                workspace_id=str(workspace_id),
                current=current_count,
                limit=memory_limit,
                plan=workspace.plan_name,
            )

            if raise_on_exceeded:
                raise QuotaExceededError(error)
            return False, error

        return True, None

    # ========================================================================
    # Feature Access Checks
    # ========================================================================

    async def check_feature_access(
        self,
        workspace_id: UUID,
        feature: str,
        raise_on_denied: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if workspace's plan includes a feature.

        Args:
            workspace_id: Workspace ID
            feature: Feature name (e.g., 'reranking', 'oauth', 'memory_agent')
            raise_on_denied: If True, raise FeatureNotAvailableError

        Returns:
            Tuple of (has_access, error_message)

        Raises:
            FeatureNotAvailableError: If raise_on_denied=True and feature not available
        """
        # Get workspace
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            error = f"Workspace {workspace_id} not found"
            if raise_on_denied:
                raise FeatureNotAvailableError(error)
            return False, error

        # Check if plan includes feature
        if not has_feature(workspace.plan_name, feature):
            # Get required plan tier
            from config.plan_tiers import get_required_plan_for_feature

            try:
                required_plan = get_required_plan_for_feature(feature)
                plan_display = PLAN_TIERS[required_plan].display_name
            except (ValueError, KeyError):
                required_plan = "unknown"
                plan_display = "higher"

            error = (
                f"Feature '{feature}' not available on {workspace.plan_name} plan. "
                f"Upgrade to {plan_display} plan to access this feature."
            )
            logger.info(
                "feature_access_denied",
                workspace_id=str(workspace_id),
                feature=feature,
                plan=workspace.plan_name,
                required_plan=required_plan,
            )

            if raise_on_denied:
                raise FeatureNotAvailableError(error)
            return False, error

        return True, None

    # ========================================================================
    # Multi-workspace Restrictions
    # ========================================================================

    async def check_workspace_creation_allowed(
        self,
        user_id: str,
        raise_on_denied: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if user can create another workspace.

        Issue #276: All users can own max 10 workspaces.
        Users can be members of additional workspaces via invites.

        Args:
            user_id: User ID
            raise_on_denied: If True, raise QuotaExceededError

        Returns:
            Tuple of (can_create, error_message)

        Raises:
            QuotaExceededError: If raise_on_denied=True and limit reached
        """
        from config.constants import MAX_OWNED_WORKSPACES_PER_USER

        # Count user's owned workspaces (where user is owner)
        workspace_count_result = await self.db.execute(
            select(func.count(Workspace.id)).where(
                Workspace.owner_user_id == user_id,
                Workspace.deleted_at.is_(None),
            )
        )
        workspace_count = workspace_count_result.scalar() or 0

        # Issue #276: Use centralized constant
        MAX_OWNED_WORKSPACES = MAX_OWNED_WORKSPACES_PER_USER

        if workspace_count >= MAX_OWNED_WORKSPACES:
            error = (
                f"Workspace limit reached. "
                f"You can own maximum {MAX_OWNED_WORKSPACES} workspaces. "
                f"You can join other workspaces as a member via invite."
            )
            logger.warning(
                "workspace_creation_denied",
                user_id=user_id,
                current_owned_workspaces=workspace_count,
                max_owned_workspaces=MAX_OWNED_WORKSPACES,
            )

            if raise_on_denied:
                raise QuotaExceededError(error)
            return False, error

        return True, None

    async def check_context_creation_allowed(
        self,
        workspace_id: UUID,
        raise_on_denied: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if workspace can create another context.

        Free plan: Max 1 context/workspace
        Basic/Pro: Unlimited contexts

        Args:
            workspace_id: Workspace ID
            raise_on_denied: If True, raise QuotaExceededError

        Returns:
            Tuple of (can_create, error_message)

        Raises:
            QuotaExceededError: If raise_on_denied=True and limit reached
        """
        # Get workspace
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            error = f"Workspace {workspace_id} not found"
            if raise_on_denied:
                raise QuotaExceededError(error)
            return False, error

        # Get plan tier limits
        plan = get_plan_tier(workspace.plan_name)

        # Count current contexts
        context_count_result = await self.db.execute(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
        context_count = context_count_result.scalar() or 0

        # Check against limit
        if context_count >= plan.max_contexts_per_workspace:
            error = (
                f"Context limit reached. "
                f"Your {plan.display_name} plan allows {plan.max_contexts_per_workspace} context(s) per workspace. "
                f"Upgrade to Basic or Pro plan for multiple contexts."
            )
            logger.warning(
                "context_creation_denied",
                workspace_id=str(workspace_id),
                current_contexts=context_count,
                max_contexts=plan.max_contexts_per_workspace,
                plan=workspace.plan_name,
            )

            if raise_on_denied:
                raise QuotaExceededError(error)
            return False, error

        return True, None

    # ========================================================================
    # Member Quota Checks (Issue #229)
    # ========================================================================

    async def check_member_quota(
        self,
        workspace_id: UUID,
        raise_on_exceeded: bool = False,
    ) -> tuple[bool, str | None]:
        """Check if workspace can invite more members.

        Counts both current members and pending (non-expired) invitations
        to prevent over-inviting.

        Args:
            workspace_id: Workspace ID
            raise_on_exceeded: If True, raise QuotaExceededError

        Returns:
            Tuple of (can_invite, error_message)

        Raises:
            QuotaExceededError: If raise_on_exceeded=True and quota exceeded

        Issue #229: Implement team member limit (10 members max for Pro plan)
        """
        # Get workspace
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            error = f"Workspace {workspace_id} not found"
            if raise_on_exceeded:
                raise QuotaExceededError(error)
            return False, error

        # Get plan limits
        plan = get_plan_tier(workspace.plan_name)

        # Count current members
        member_count_result = await self.db.execute(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
        member_count = member_count_result.scalar() or 0

        # Count pending invitations (not accepted, not expired)
        pending_count_result = await self.db.execute(
            select(func.count(WorkspaceInvitation.id)).where(
                WorkspaceInvitation.workspace_id == workspace_id,
                WorkspaceInvitation.accepted_at.is_(None),
                or_(
                    WorkspaceInvitation.expires_at.is_(None),
                    WorkspaceInvitation.expires_at > utcnow(),
                ),
            )
        )
        pending_count = pending_count_result.scalar() or 0

        total_used = member_count + pending_count

        # Check limit
        if total_used >= plan.max_members_per_workspace:
            error = (
                f"Member limit reached. "
                f"Your {plan.display_name} plan allows {plan.max_members_per_workspace} member(s) per workspace. "
                f"Current members: {member_count}, Pending invitations: {pending_count}. "
                f"Upgrade to add more members."
            )
            logger.warning(
                "member_quota_exceeded",
                workspace_id=str(workspace_id),
                member_count=member_count,
                pending_count=pending_count,
                total_used=total_used,
                limit=plan.max_members_per_workspace,
                plan=workspace.plan_name,
            )

            if raise_on_exceeded:
                raise QuotaExceededError(error)
            return False, error

        return True, None

    # ========================================================================
    # Quota Status
    # ========================================================================

    async def get_quota_status(self, workspace_id: UUID) -> dict[str, Any]:
        """Get comprehensive quota status for workspace.

        Returns current usage, limits, and warning flags.

        Args:
            workspace_id: Workspace ID

        Returns:
            Dict with quota status:
                - memory: {current, limit, percentage, warning, exceeded}
                - features: {reranking, oauth, memory_agent} (bool)
        """
        # Get workspace
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            return {}

        # Get plan tier
        plan = get_plan_tier(workspace.plan_name)

        # Get all member user_ids
        members_result = await self.db.execute(
            select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == workspace_id)
        )
        member_ids = [row[0] for row in members_result.all()]

        # Calculate memory usage
        # Issue #273 C-2: Add NULL workspace_id and deleted_at filters to prevent quota bypass
        if member_ids:
            memory_count_result = await self.db.execute(
                select(func.count(Memory.id)).where(
                    Memory.user_id.in_(member_ids),
                    Memory.workspace_id.isnot(
                        None
                    ),  # Exclude NULL workspace_id (orphaned memories)
                    Memory.deleted_at.is_(None),  # Exclude soft-deleted memories
                )
            )
            memory_count = memory_count_result.scalar() or 0
        else:
            memory_count = 0

        # Calculate percentages
        memory_percentage = (
            (memory_count / workspace.memory_limit * 100) if workspace.memory_limit > 0 else 0
        )

        return {
            "memory": {
                "current": memory_count,
                "limit": workspace.memory_limit,
                "percentage": round(memory_percentage, 2),
                "warning": memory_percentage >= 80,
                "exceeded": memory_percentage >= 100,
            },
            "features": {
                "reranking": "reranking" in plan.features,
                "oauth": "oauth" in plan.features,
                "memory_agent": "memory_agent" in plan.features,
            },
            "plan": {
                "name": workspace.plan_name,
                "display_name": plan.display_name,
            },
        }
