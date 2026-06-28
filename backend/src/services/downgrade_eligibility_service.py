"""Usage-fit plan-downgrade eligibility (#1123).

memory-cloud is the usage source of truth, so a plan DOWNGRADE must be guarded:
purchased addons are KEPT, but a tier downgrade is only allowed when current
usage fits the TARGET tier's *effective* limit (target tier base + the
workspace's retained addon bonuses). Dimensions that exceed the target are
"blockers" — each names the destructive cleanup needed before the downgrade can
apply (remove members / delete contexts / delete memories / revoke resource
tokens / unshare contexts / remove connectors / disable sleep contexts / delete
files).

This service is READ-ONLY. It reports eligibility + per-dimension blockers for
every tier strictly below the workspace's current tier. The external billing
service gates its portal downgrade UI on this read; the absolute ``/internal``
plan push stays reconcile-safe and does not itself enforce the guard (#1096 made
memory-cloud Stripe-agnostic — enforcement is the portal's responsibility,
informed by this read).

Each count is workspace-scoped and matches its write-time enforcement basis so
the eligibility verdict agrees with the live quota gates.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import PLAN_TIERS, PlanTier
from models.auth import Context, Workspace, WorkspaceMember, _zero_floor
from models.file_objects import FileObject
from models.memory import Memory
from models.resource import ResourceToken, WorkspaceConnector

# Lowest → highest tier. A downgrade target is any tier strictly left of the
# workspace's current tier; mirrors ``plan_order`` in workspace_plan.py.
_PLAN_ORDER: list[str] = ["free", "basic", "pro"]


class DowngradeDimension(StrEnum):
    """Usage dimensions that can block a downgrade (stable wire contract)."""

    MEMBERS = "members"
    CONTEXTS = "contexts"
    MEMORIES = "memories"
    RESOURCE_TOKENS = "resource_tokens"
    SHARED_CONTEXTS = "shared_contexts"
    CONNECTORS = "connectors"
    SLEEP_ENABLED_CONTEXTS = "sleep_enabled_contexts"
    STORAGE_BYTES = "storage_bytes"


class DowngradeCleanup(StrEnum):
    """The destructive cleanup a blocker requires (stable wire contract)."""

    REMOVE_MEMBERS = "remove_members"
    DELETE_CONTEXTS = "delete_contexts"
    DELETE_MEMORIES = "delete_memories"
    REVOKE_RESOURCE_TOKENS = "revoke_resource_tokens"
    UNSHARE_CONTEXTS = "unshare_contexts"
    REMOVE_CONNECTORS = "remove_connectors"
    DISABLE_SLEEP_CONTEXTS = "disable_sleep_contexts"
    DELETE_FILES = "delete_files"


class DowngradeBlocker(BaseModel):
    """One usage dimension that blocks (or requires cleanup before) a downgrade.

    ``overage`` is how much must be shed to fit the target. For ``shared_contexts``
    (a tier feature, not a numeric cap) the limit is 0 and ``overage`` equals the
    full shared-context count — all of them must be unshared. ``storage_bytes`` is
    in bytes.
    """

    dimension: DowngradeDimension
    usage: int
    limit: int
    overage: int
    cleanup: DowngradeCleanup


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
    connectors: int
    sleep_enabled_contexts: int
    storage_bytes: int


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
        """Count current workspace-scoped usage across every guarded dimension.

        Each count mirrors its write-time enforcement basis so the verdict agrees
        with the live quota gates. Pending invitations are intentionally excluded
        from ``members`` — they consume no seat until accepted and are re-gated at
        accept-time, so they do not block a downgrade of the current membership.
        """
        members = await self._scalar(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
        contexts = await self._scalar(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
        # Shared == non-private (is_private is False). Shared contexts are a Pro
        # feature; on a downgrade to a tier that disallows them they must all be
        # made private again.
        shared_contexts = await self._scalar(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.is_private.is_(False),
                Context.deleted_at.is_(None),
            )
        )
        # Memories belonging to THIS workspace (the per-workspace memory_limit is
        # what a downgrade must fit, and the cleanup must be actionable here). NB:
        # the live memory-quota gate (quota_service) joins on member user_id and
        # can over-count a multi-workspace member's memories across workspaces —
        # an orthogonal #273 quirk; the eligibility read uses the precise
        # per-workspace count.
        memories = await self._scalar(
            select(func.count(Memory.id)).where(
                Memory.workspace_id == workspace_id,
                Memory.deleted_at.is_(None),
            )
        )
        # Workspace-scoped active resource tokens, excluding connector-owned
        # tokens (mirrors the create-time count in resource_tokens.py: connector
        # tokens are gated by max_connectors, not max_resource_tokens). The
        # ``workspace_id`` column is a Phase-1 shadow (#323) still nullable, so a
        # legacy token with NULL workspace_id is not counted here — acceptable
        # for an advisory eligibility read while the backfill (#324/#325) lands.
        resource_tokens = await self._scalar(
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
        # Active ai-worker connectors (#850). WorkspaceConnector has no status
        # column — every row is an active seat.
        connectors = await self._scalar(
            select(func.count(WorkspaceConnector.id)).where(
                WorkspaceConnector.workspace_id == workspace_id
            )
        )
        # Sleep-enabled contexts (#560): sleep_mode anything but "skip".
        sleep_enabled_contexts = await self._scalar(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
                Context.sleep_mode != "skip",
            )
        )
        # Committed file storage in bytes (#485): only "uploaded" (not reserved/
        # failed) and not soft-deleted, matching storage_quota_service's DB
        # aggregate. A direct aggregate (not the Redis-cached value) keeps the
        # eligibility verdict authoritative.
        storage_bytes = await self._scalar(
            select(func.coalesce(func.sum(FileObject.size_bytes), 0)).where(
                FileObject.workspace_id == workspace_id,
                FileObject.status == "uploaded",
                FileObject.deleted_at.is_(None),
            )
        )
        return WorkspaceUsage(
            members=members,
            contexts=contexts,
            shared_contexts=shared_contexts,
            memories=memories,
            resource_tokens=resource_tokens,
            connectors=connectors,
            sleep_enabled_contexts=sleep_enabled_contexts,
            storage_bytes=storage_bytes,
        )

    async def _scalar(self, stmt) -> int:
        return (await self.db.execute(stmt)).scalar() or 0

    def _evaluate_tier(
        self, workspace: Workspace, usage: WorkspaceUsage, tier: PlanTier
    ) -> TierDowngradeEligibility:
        """Compare usage against one target tier's effective limits (addons kept)."""
        blockers: list[DowngradeBlocker] = []

        def check(dimension, used, limit, cleanup):
            if used > limit:
                blockers.append(
                    DowngradeBlocker(
                        dimension=dimension,
                        usage=used,
                        limit=limit,
                        overage=used - limit,
                        cleanup=cleanup,
                    )
                )

        # Numeric caps: target effective limit = _zero_floor(target base, kept
        # addon bonus). Reusing _zero_floor keeps the #569 zero-base rule (a
        # zero-base tier never stacks addons) consistent with the live quotas.
        check(
            DowngradeDimension.MEMBERS,
            usage.members,
            _zero_floor(tier.max_members_per_workspace, workspace.addon_member_bonus),
            DowngradeCleanup.REMOVE_MEMBERS,
        )
        check(
            DowngradeDimension.CONTEXTS,
            usage.contexts,
            _zero_floor(tier.max_contexts_per_workspace, workspace.addon_context_bonus),
            DowngradeCleanup.DELETE_CONTEXTS,
        )
        check(
            DowngradeDimension.MEMORIES,
            usage.memories,
            _zero_floor(tier.memory_limit, workspace.addon_memory_bonus),
            DowngradeCleanup.DELETE_MEMORIES,
        )
        # Resource tokens are tier-fixed (no addon → effective == base).
        check(
            DowngradeDimension.RESOURCE_TOKENS,
            usage.resource_tokens,
            tier.max_resource_tokens,
            DowngradeCleanup.REVOKE_RESOURCE_TOKENS,
        )
        check(
            DowngradeDimension.CONNECTORS,
            usage.connectors,
            _zero_floor(tier.max_connectors, workspace.addon_connector_bonus),
            DowngradeCleanup.REMOVE_CONNECTORS,
        )
        check(
            DowngradeDimension.SLEEP_ENABLED_CONTEXTS,
            usage.sleep_enabled_contexts,
            _zero_floor(tier.sleep_enabled_contexts_limit, workspace.addon_sleep_contexts_bonus),
            DowngradeCleanup.DISABLE_SLEEP_CONTEXTS,
        )
        # Storage addon is stored in MB; scale to bytes before the zero-floor so
        # the check happens on the final unit (matches effective_storage_limit_bytes).
        storage_addon_bytes = (workspace.addon_storage_bonus_mb or 0) * 1024 * 1024
        check(
            DowngradeDimension.STORAGE_BYTES,
            usage.storage_bytes,
            _zero_floor(tier.storage_limit_bytes, storage_addon_bytes),
            DowngradeCleanup.DELETE_FILES,
        )

        # Shared contexts are a tier FEATURE, not a numeric cap: a target tier
        # that disallows shared contexts requires unsharing all of them.
        if not tier.allows_shared_contexts and usage.shared_contexts > 0:
            blockers.append(
                DowngradeBlocker(
                    dimension=DowngradeDimension.SHARED_CONTEXTS,
                    usage=usage.shared_contexts,
                    limit=0,
                    overage=usage.shared_contexts,
                    cleanup=DowngradeCleanup.UNSHARE_CONTEXTS,
                )
            )

        return TierDowngradeEligibility(
            target_plan=tier.name,
            eligible=not blockers,
            blockers=blockers,
        )
