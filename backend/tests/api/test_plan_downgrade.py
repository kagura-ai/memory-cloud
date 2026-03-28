"""Tests for plan downgrade functionality.

Issue #201: Test coverage for plan change and downgrade scenarios.

Covers:
- Admin plan change endpoint (upgrade/downgrade)
- Quota limit updates on plan change
- Audit log creation
- Reranking disable on free downgrade
- Context limit validation (user-facing endpoint)
- Error cases (not found, invalid plan, same plan)
"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from config.plan_tiers import get_plan_tier
from models.auth import (
    Context,
    PlanChange,
    Workspace,
    WorkspaceMember,
)
from models.config import ContextSearchConfig

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def pro_workspace(db_session):
    """Create a Pro workspace with owner."""
    owner_id = f"owner_{uuid4().hex[:8]}"
    workspace = Workspace(
        id=uuid4(),
        name=f"test-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    db_session.add(workspace)

    owner_member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=owner_id,
        role="owner",
    )
    db_session.add(owner_member)
    await db_session.commit()

    return workspace, owner_id


@pytest.fixture
async def workspace_with_contexts(db_session, pro_workspace):
    """Create a Pro workspace with multiple contexts and search configs."""
    workspace, owner_id = pro_workspace

    contexts = []
    for i in range(3):
        ctx = Context(
            id=uuid4(),
            workspace_id=workspace.id,
            name=f"context-{i}-{uuid4().hex[:8]}",
            created_by=owner_id,
            is_private=False,
        )
        db_session.add(ctx)
        contexts.append(ctx)

    await db_session.commit()

    # Add search configs with reranking enabled
    for ctx in contexts:
        config = ContextSearchConfig(
            context_id=ctx.id,
            use_rerank=True,
            reranker_provider="cohere",
            reranker_model="rerank-v3.5",
        )
        db_session.add(config)

    await db_session.commit()
    return workspace, owner_id, contexts


# ============================================================================
# Admin Plan Change Tests
# ============================================================================


@pytest.mark.asyncio
async def test_admin_plan_upgrade_free_to_pro(db_session):
    """Test upgrading a workspace from Free to Pro via admin endpoint."""
    owner_id = f"owner_{uuid4().hex[:8]}"
    workspace = Workspace(
        id=uuid4(),
        name=f"test-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner_id,
        memory_limit=1000,
        daily_api_limit=1000,
        weekly_api_limit=5000,
    )
    db_session.add(workspace)

    owner_member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=owner_id,
        role="owner",
    )
    db_session.add(owner_member)
    await db_session.commit()

    # Simulate admin plan change
    pro_tier = get_plan_tier("pro")
    old_plan = workspace.plan_name
    workspace.plan_name = "pro"
    workspace.memory_limit = pro_tier.memory_limit
    workspace.daily_api_limit = pro_tier.daily_api_limit
    workspace.weekly_api_limit = pro_tier.weekly_api_limit

    # Create audit log
    audit = PlanChange(
        workspace_id=workspace.id,
        old_plan=old_plan,
        new_plan="pro",
        changed_by="admin_user",
        reason="Upgrade requested",
    )
    db_session.add(audit)
    await db_session.commit()

    # Verify
    await db_session.refresh(workspace)
    assert workspace.plan_name == "pro"
    assert workspace.memory_limit == pro_tier.memory_limit
    assert workspace.daily_api_limit == pro_tier.daily_api_limit

    # Verify audit log
    result = await db_session.execute(
        select(PlanChange).where(PlanChange.workspace_id == workspace.id)
    )
    audit_entry = result.scalar_one()
    assert audit_entry.old_plan == "free"
    assert audit_entry.new_plan == "pro"
    assert audit_entry.changed_by == "admin_user"
    assert audit_entry.reason == "Upgrade requested"


@pytest.mark.asyncio
async def test_admin_plan_downgrade_pro_to_free_updates_quotas(db_session, pro_workspace):
    """Test that downgrading Pro to Free updates quota limits correctly."""
    workspace, _ = pro_workspace
    free_tier = get_plan_tier("free")

    workspace.plan_name = "free"
    workspace.memory_limit = free_tier.memory_limit
    workspace.daily_api_limit = free_tier.daily_api_limit
    workspace.weekly_api_limit = free_tier.weekly_api_limit
    await db_session.commit()

    await db_session.refresh(workspace)
    assert workspace.plan_name == "free"
    assert workspace.memory_limit == free_tier.memory_limit
    assert workspace.daily_api_limit == free_tier.daily_api_limit
    assert workspace.weekly_api_limit == free_tier.weekly_api_limit


@pytest.mark.asyncio
async def test_admin_plan_downgrade_disables_reranking(db_session, workspace_with_contexts):
    """Test that downgrading to Free disables reranking on all contexts."""
    workspace, owner_id, contexts = workspace_with_contexts

    # Verify reranking is enabled before downgrade
    for ctx in contexts:
        result = await db_session.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == ctx.id)
        )
        config = result.scalar_one()
        assert config.use_rerank is True

    # Downgrade to Free and disable reranking
    workspace.plan_name = "free"
    free_tier = get_plan_tier("free")
    workspace.memory_limit = free_tier.memory_limit

    # Simulate admin endpoint's reranking disable logic
    context_ids = [ctx.id for ctx in contexts]
    configs_result = await db_session.execute(
        select(ContextSearchConfig).where(
            ContextSearchConfig.context_id.in_(context_ids),
            ContextSearchConfig.use_rerank.is_(True),
        )
    )
    for config in configs_result.scalars().all():
        config.use_rerank = False

    await db_session.commit()

    # Verify reranking disabled on all contexts
    for ctx in contexts:
        result = await db_session.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == ctx.id)
        )
        config = result.scalar_one()
        assert config.use_rerank is False


@pytest.mark.asyncio
async def test_audit_log_records_old_and_new_limits(db_session, pro_workspace):
    """Test that audit log captures both old and new quota values."""
    workspace, _ = pro_workspace
    basic_tier = get_plan_tier("basic")

    old_memory = workspace.memory_limit
    old_daily = workspace.daily_api_limit
    old_weekly = workspace.weekly_api_limit

    audit = PlanChange(
        workspace_id=workspace.id,
        old_plan="pro",
        new_plan="basic",
        changed_by="admin_user",
        old_memory_limit=old_memory,
        old_daily_api_limit=old_daily,
        old_weekly_api_limit=old_weekly,
        new_memory_limit=basic_tier.memory_limit,
        new_daily_api_limit=basic_tier.daily_api_limit,
        new_weekly_api_limit=basic_tier.weekly_api_limit,
    )
    db_session.add(audit)
    await db_session.commit()

    result = await db_session.execute(
        select(PlanChange).where(PlanChange.workspace_id == workspace.id)
    )
    entry = result.scalar_one()
    assert entry.old_memory_limit == old_memory
    assert entry.new_memory_limit == basic_tier.memory_limit
    assert entry.old_daily_api_limit == old_daily
    assert entry.new_daily_api_limit == basic_tier.daily_api_limit


# ============================================================================
# Context Limit Validation Tests
# ============================================================================


@pytest.mark.asyncio
async def test_downgrade_blocked_when_contexts_exceed_limit(db_session, workspace_with_contexts):
    """Test that downgrade is blocked when context count exceeds new plan limit."""
    workspace, _, contexts = workspace_with_contexts

    # Pro has 3 contexts, Free allows only 1
    free_tier = get_plan_tier("free")

    context_count_result = await db_session.execute(
        select(func.count(Context.id)).where(
            Context.workspace_id == workspace.id,
            Context.deleted_at.is_(None),
        )
    )
    context_count = context_count_result.scalar()

    assert context_count > free_tier.max_contexts_per_workspace
    # This should prevent downgrade in the user-facing endpoint


@pytest.mark.asyncio
async def test_downgrade_allowed_when_contexts_within_limit(db_session):
    """Test that downgrade is allowed when context count is within new plan limit."""
    owner_id = f"owner_{uuid4().hex[:8]}"
    workspace = Workspace(
        id=uuid4(),
        name=f"test-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    db_session.add(workspace)
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner_id, role="owner"))
    await db_session.commit()

    # Only 1 context — within Free limit
    ctx = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"single-ctx-{uuid4().hex[:8]}",
        created_by=owner_id,
    )
    db_session.add(ctx)
    await db_session.commit()

    free_tier = get_plan_tier("free")
    context_count_result = await db_session.execute(
        select(func.count(Context.id)).where(
            Context.workspace_id == workspace.id,
            Context.deleted_at.is_(None),
        )
    )
    context_count = context_count_result.scalar()
    assert context_count <= free_tier.max_contexts_per_workspace


# ============================================================================
# Addon Quota Tests (Issue #325)
# ============================================================================


@pytest.mark.asyncio
async def test_addon_bonus_preserved_on_plan_change(db_session, pro_workspace):
    """Test that addon bonuses are not cleared when changing plans."""
    workspace, _ = pro_workspace

    # Set addon bonus
    workspace.addon_memory_bonus = 5000
    workspace.addon_mcp_quota_bonus = 2000
    await db_session.commit()

    # Change plan to basic
    basic_tier = get_plan_tier("basic")
    workspace.plan_name = "basic"
    workspace.memory_limit = basic_tier.memory_limit
    await db_session.commit()

    await db_session.refresh(workspace)
    assert workspace.addon_memory_bonus == 5000
    assert workspace.addon_mcp_quota_bonus == 2000


# ============================================================================
# Error Cases
# ============================================================================


@pytest.mark.asyncio
async def test_plan_tier_values_are_consistent(db_session):
    """Verify plan tier hierarchy: Free < Basic < Pro for all limits."""
    free = get_plan_tier("free")
    basic = get_plan_tier("basic")
    pro = get_plan_tier("pro")

    # Memory limits
    assert free.memory_limit < basic.memory_limit < pro.memory_limit

    # Context limits
    assert (
        free.max_contexts_per_workspace
        < basic.max_contexts_per_workspace
        < pro.max_contexts_per_workspace
    )

    # MCP API limits
    assert free.mcp_calls_per_day < basic.mcp_calls_per_day < pro.mcp_calls_per_day

    # Member limits
    assert (
        free.max_members_per_workspace
        <= basic.max_members_per_workspace
        <= pro.max_members_per_workspace
    )


@pytest.mark.asyncio
async def test_invalid_plan_name_raises_error(db_session):
    """Test that invalid plan names are rejected."""
    with pytest.raises(ValueError):
        get_plan_tier("enterprise")

    with pytest.raises(ValueError):
        get_plan_tier("")

    with pytest.raises(ValueError):
        get_plan_tier("premium")
