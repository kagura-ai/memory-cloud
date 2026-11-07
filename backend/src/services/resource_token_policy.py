"""Shared Resource Token policy; callers retain their own authentication boundary."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.resource_tokens import (
    count_regular_active_tokens_for_mint,
    workspace_regular_active_tokens,
)
from models.resource import ResourceToken, WorkspaceConnector
from services.resource_lookup import resolve_resource_pk
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError


async def check_workspace_quota_ceiling(
    db: AsyncSession, token: ResourceToken, workspace_id: UUID, new_quota: int
) -> None:
    """Refuse a quota raise that takes the workspace over its plan ceiling (#1877).

    The ceiling is ``max_resource_tokens * 10000`` events/hour and the budget
    is the WORKSPACE's: every other active token in it counts, whoever minted
    it — the same population the create-time count cap is taken over
    (``auth.resource_tokens.workspace_regular_active_tokens``, #1919). The sum used to be over
    ``created_by == caller``, and the cap per creator, so two owners could
    mint their way over the ceiling and then no raise fitted.

    Connector-owned tokens are outside this budget, consistent with the
    create-time cap (#858): a connector is gated by ``max_connectors`` seats
    and its token never takes a ``max_resource_tokens`` slot, so it neither
    counts toward the sum nor is checked against it when it is the target
    (its quota stays bounded by the per-token maximum of the request model).

    Raises:
        HTTPException: 400 when the raise does not fit.
    """
    from config.plan_tiers import get_plan_tier
    from models.auth import Workspace

    await count_regular_active_tokens_for_mint(db, workspace_id)

    if token.resource_pk is not None:
        connector_result = await db.execute(
            select(WorkspaceConnector.id).where(WorkspaceConnector.resource_pk == token.resource_pk)
        )
        if connector_result.scalar_one_or_none() is not None:
            return

    workspace_result = await db.execute(
        select(Workspace.plan_name).where(Workspace.id == workspace_id)
    )
    plan_name = workspace_result.scalar_one_or_none()
    if not plan_name:
        return

    max_total_quota = get_plan_tier(plan_name).max_resource_tokens * 10000

    # Quota used by the workspace's OTHER regular tokens.
    other_tokens_result = await db.execute(
        workspace_regular_active_tokens(
            workspace_id, func.sum(ResourceToken.quota_events_per_hour)
        ).where(ResourceToken.id != token.id)
    )
    used_by_others = other_tokens_result.scalar() or 0

    if used_by_others + new_quota > max_total_quota:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Quota limit exceeded. Total quota (including this update) would be {used_by_others + new_quota}, but plan allows {max_total_quota}.",
        )


async def authorize_resource_token_mint(
    db: AsyncSession, workspace_id: UUID, resource_id: str
) -> UUID:
    """Validate live binding, feature and workspace cap under the shared mint lock."""
    # Check plan limits and active token count
    from config.plan_tiers import (
        get_plan_tier,
        has_feature,
        lowest_tier_with_limit,
        plan_display_name,
        quota_gate_details,
    )
    from models.auth import Context, Workspace

    # SECURITY: Verify the resource belongs to current workspace
    # Issue #268: Workspace boundary violation prevention. #1863: judged by
    # the ``resources`` row like list/update/revoke; minting additionally
    # needs a live context — a retired resource gets no new credentials —
    # and the message says which of the two is missing (the resource may
    # well be listed, its existing tokens being reachable while active).
    resource_pk = await resolve_resource_pk(db, workspace_id, resource_id)
    if resource_pk is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Resource ID '{resource_id}' not found in your workspace or you don't have access to it.",
        )
    context_result = await db.execute(
        select(Context.id).where(
            and_(
                Context.resource_id == resource_id,
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
    )
    if context_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Resource ID '{resource_id}' has no live context in your "
                "workspace; restore or create a context bound to it before creating "
                "tokens."
            ),
        )

    # ``WorkspaceOwner`` guarantees a workspace id; a missing plan row is a
    # data gap that must fail CLOSED (no plan → no feature), not skip the
    # gate the way the old ``if workspace_id: … if plan_name:`` nesting did.
    workspace_result = await db.execute(
        select(Workspace.plan_name).where(Workspace.id == workspace_id)
    )
    plan_name = workspace_result.scalar_one_or_none()

    # Issue #1551: "may create" is the feature flag (XL-only), NOT
    # ``max_resource_tokens == 0`` — M/L keep a positive cap so the
    # tokens they already hold stay editable and served. The count
    # check below remains the second gate for tiers with the feature.
    if not has_feature(plan_name or "", "resources"):
        raise FeatureNotAvailableError.for_feature(plan_name, "resources")
    plan = get_plan_tier(plan_name)

    # Check the workspace's active token count against the cap (#858
    # excludes connector-owned tokens, #1919 counts the workspace, not the
    # caller — see ``workspace_regular_active_tokens``). The count runs
    # under the workspace's mint lock (#1927), held until the commit below,
    # so a concurrent mint for the same workspace counts this token.
    active_count = await count_regular_active_tokens_for_mint(db, workspace_id)

    if active_count >= plan.max_resource_tokens:
        # #1644 S5: the cap keeps its 403 — the status is what existing
        # clients branch on — but answers the documented ``QUOTA-001``
        # envelope instead of the non-semantic ``HTTP-403`` placeholder.
        # ``plan_name.upper()`` rendered a third tier vocabulary
        # ("PROMAX") beside the keys and the display names.
        raise QuotaExceededError(
            (
                f"Token limit reached. Your {plan_display_name(plan_name)} plan allows "
                f"{plan.max_resource_tokens} active tokens. "
                "Please revoke unused tokens or upgrade your plan."
            ),
            status_code=status.HTTP_403_FORBIDDEN,
            **quota_gate_details(
                plan_name,
                "resource_tokens",
                current=active_count,
                limit=plan.max_resource_tokens,
                required_plan=lowest_tier_with_limit(
                    "max_resource_tokens", plan.max_resource_tokens
                ),
                feature="resources",
            ),
        )

    return resource_pk
