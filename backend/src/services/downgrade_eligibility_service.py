"""Usage-fit plan-downgrade eligibility (#1123).

memory-cloud is the usage source of truth, so a plan DOWNGRADE must be guarded:
purchased addons are KEPT, but a tier downgrade is only allowed when current
usage fits the TARGET tier's *effective* limit (target tier base + the
workspace's retained addon bonuses). Dimensions that exceed the target are
"blockers" — each names the destructive cleanup needed before the downgrade can
apply (remove members / delete contexts / delete memories / revoke resource
tokens / unshare contexts).

This service is READ-ONLY. It reports eligibility + per-dimension blockers for
every tier strictly below the workspace's current tier. The external billing
service gates its portal downgrade UI on this read; the absolute ``/internal``
plan push stays reconcile-safe and does not itself enforce the guard (#1096 made
memory-cloud Stripe-agnostic — enforcement is the portal's responsibility,
informed by this read).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import PLAN_TIERS, PlanTier
from models.auth import Context, Workspace, WorkspaceMember, _zero_floor
from models.memory import Memory
from models.resource import ResourceToken, WorkspaceConnector

# Lowest → highest tier. A downgrade target is any tier strictly left of the
# workspace's current tier; mirrors ``plan_order`` in workspace_plan.py.
_PLAN_ORDER: list[str] = ["free", "basic", "pro"]


class DowngradeBlocker(BaseModel):
    """One usage dimension that blocks (or requires cleanup before) a downgrade.

    ``overage`` is how much must be shed to fit the target. For ``shared_contexts``
    (a tier feature, not a numeric cap) the limit is 0 and ``overage`` equals the
    full shared-context count — all of them must be unshared.
    """

    dimension: str  # members | contexts | memories | resource_tokens | shared_contexts
    usage: int
    limit: int
    overage: int
    cleanup: str  # remove_members | delete_contexts | delete_memories |
    #             # revoke_resource_tokens | unshare_contexts


class TierDowngradeEligibility(BaseModel):
    """Whether the workspace's usage fits one specific lower tier."""

    target_plan: str
    eligible: bool
    blockers: list[DowngradeBlocker]


class WorkspaceUsage(BaseModel):
    """Current workspace-scoped usage counts used for the fit check."""

    members: int
    contexts: int
    shared_contexts: int
    memories: int
    resource_tokens: int


class DowngradeEligibilityService:
    """Evaluate whether a workspace can downgrade to each lower tier."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def evaluate(self, workspace: Workspace) -> list[TierDowngradeEligibility]:
        """Eligibility for every tier strictly below the workspace's current tier.

        Returns an empty list when the workspace is already on the lowest tier
        (or on an unknown tier — fail-closed to "no downgrade targets").
        """
        current_index = (
            _PLAN_ORDER.index(workspace.plan_name) if workspace.plan_name in _PLAN_ORDER else 0
        )
        if current_index == 0:
            return []

        usage = await self.current_usage(workspace.id)
        return [
            self._evaluate_tier(workspace, usage, PLAN_TIERS[target])
            for target in _PLAN_ORDER[:current_index]
        ]

    async def current_usage(self, workspace_id: UUID) -> WorkspaceUsage:
        """Count current workspace-scoped usage across every guarded dimension."""
        members = await self._count(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
        contexts = await self._count(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
        # Shared == non-private (is_private is False). Shared contexts are a Pro
        # feature; on a downgrade to a tier that disallows them they must all be
        # made private again.
        shared_contexts = await self._count(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.is_private.is_(False),
                Context.deleted_at.is_(None),
            )
        )
        # Match the ENFORCEMENT count (quota_service.check_memory_quota): memories
        # created by this workspace's members, with a non-null workspace_id, not
        # soft-deleted. Using the same basis keeps the eligibility verdict in
        # agreement with the live memory-quota gate (and the #1121 plan display).
        memories = await self._count(
            select(func.count(Memory.id))
            .select_from(Memory)
            .join(WorkspaceMember, Memory.user_id == WorkspaceMember.user_id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                Memory.workspace_id.isnot(None),
                Memory.deleted_at.is_(None),
            )
        )
        # Workspace-scoped active resource tokens, excluding connector-owned
        # tokens (mirrors the create-time count in resource_tokens.py: connector
        # tokens are gated by max_connectors, not max_resource_tokens). The
        # ``workspace_id`` column is a Phase-1 shadow (#323) still nullable, so a
        # legacy token with NULL workspace_id is not counted here — acceptable
        # for an advisory eligibility read while the backfill (#324/#325) lands.
        resource_tokens = await self._count(
            select(func.count(ResourceToken.id))
            .outerjoin(
                WorkspaceConnector,
                WorkspaceConnector.resource_pk == ResourceToken.resource_pk,
            )
            .where(
                ResourceToken.workspace_id == workspace_id,
                ResourceToken.is_active.is_(True),
                WorkspaceConnector.id.is_(None),
            )
        )
        return WorkspaceUsage(
            members=members,
            contexts=contexts,
            shared_contexts=shared_contexts,
            memories=memories,
            resource_tokens=resource_tokens,
        )

    async def _count(self, stmt) -> int:
        return (await self.db.execute(stmt)).scalar() or 0

    def _evaluate_tier(
        self, workspace: Workspace, usage: WorkspaceUsage, tier: PlanTier
    ) -> TierDowngradeEligibility:
        """Compare usage against one target tier's effective limits (addons kept)."""
        blockers: list[DowngradeBlocker] = []

        # Numeric caps: target effective limit = _zero_floor(target base, kept
        # addon bonus). Reusing _zero_floor keeps the #569 zero-base rule (a
        # zero-base tier never stacks addons) consistent with the live quotas.
        members_limit = _zero_floor(tier.max_members_per_workspace, workspace.addon_member_bonus)
        if usage.members > members_limit:
            blockers.append(
                DowngradeBlocker(
                    dimension="members",
                    usage=usage.members,
                    limit=members_limit,
                    overage=usage.members - members_limit,
                    cleanup="remove_members",
                )
            )

        contexts_limit = _zero_floor(tier.max_contexts_per_workspace, workspace.addon_context_bonus)
        if usage.contexts > contexts_limit:
            blockers.append(
                DowngradeBlocker(
                    dimension="contexts",
                    usage=usage.contexts,
                    limit=contexts_limit,
                    overage=usage.contexts - contexts_limit,
                    cleanup="delete_contexts",
                )
            )

        memories_limit = _zero_floor(tier.memory_limit, workspace.addon_memory_bonus)
        if usage.memories > memories_limit:
            blockers.append(
                DowngradeBlocker(
                    dimension="memories",
                    usage=usage.memories,
                    limit=memories_limit,
                    overage=usage.memories - memories_limit,
                    cleanup="delete_memories",
                )
            )

        # Resource tokens are tier-fixed (no addon → effective == base).
        tokens_limit = tier.max_resource_tokens
        if usage.resource_tokens > tokens_limit:
            blockers.append(
                DowngradeBlocker(
                    dimension="resource_tokens",
                    usage=usage.resource_tokens,
                    limit=tokens_limit,
                    overage=usage.resource_tokens - tokens_limit,
                    cleanup="revoke_resource_tokens",
                )
            )

        # Shared contexts are a tier FEATURE, not a numeric cap: a target tier
        # that disallows shared contexts requires unsharing all of them.
        if not tier.allows_shared_contexts and usage.shared_contexts > 0:
            blockers.append(
                DowngradeBlocker(
                    dimension="shared_contexts",
                    usage=usage.shared_contexts,
                    limit=0,
                    overage=usage.shared_contexts,
                    cleanup="unshare_contexts",
                )
            )

        return TierDowngradeEligibility(
            target_plan=tier.name,
            eligible=not blockers,
            blockers=blockers,
        )
