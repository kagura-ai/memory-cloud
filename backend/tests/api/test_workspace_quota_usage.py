"""Complete quota meters preserve effective caps and workspace boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from api.routes import workspace_quota_usage as meters
from api.routes.usage import CurrentUsage, PlanLimits, WorkspacesUsage
from config.plan_tiers import get_plan_tier
from models.auth import Workspace


def payload(workspace):
    tier = get_plan_tier(workspace.plan_name)
    plan = PlanLimits(
        plan_name=workspace.plan_name,
        memory_limit=workspace.effective_memory_limit,
        memories_per_day=workspace.effective_memories_per_day,
        daily_total_limit=0,
        weekly_total_limit=0,
        **{
            f"{mode}_calls_per_{period}": getattr(workspace, f"effective_{mode}_calls_per_{period}")
            for mode in ("mcp", "rest", "public")
            for period in ("day", "week")
        },
    )
    usage = CurrentUsage(
        memory_count=80,
        memories_created_today=10,
        api_calls_today=20,
        api_calls_this_week=50,
        mcp_calls_today=20,
        mcp_calls_this_week=50,
        workspaces=WorkspacesUsage(used=2, limit=3, remaining=1),
    )
    return plan, usage, tier


@pytest.fixture
def dependencies(monkeypatch):
    monkeypatch.setattr(meters, "get_current_storage_usage", AsyncMock(return_value=1234))
    monkeypatch.setattr(meters, "_get_user_timezone", AsyncMock(return_value="Asia/Tokyo"))
    monkeypatch.setattr(meters, "get_today_analysis_count", AsyncMock(return_value=2))
    monkeypatch.setattr(
        meters,
        "get_settings",
        lambda: SimpleNamespace(enable_byok=False, enable_cost_display=False),
    )
    monkeypatch.setattr(meters, "content_chunks_gated", lambda: True)
    monkeypatch.setattr(meters, "llm_selector_gated", lambda: True)
    db = AsyncMock()
    db.execute.side_effect = [
        MagicMock(
            one=lambda: SimpleNamespace(
                contexts=2, members=3, connectors=1, content_chunks=1, llm_selector=0
            )
        ),
        MagicMock(one=lambda: SimpleNamespace(token_count=2, capacity=2000)),
    ]
    return db


@pytest.mark.asyncio
async def test_effective_limits_and_all_meter_dimensions(dependencies):
    workspace = Workspace(
        id=uuid4(),
        plan_name="pro",
        addon_context_bonus=3,
        addon_storage_bonus_mb=10,
        addon_mcp_quota_bonus=100,
    )
    plan, usage, tier = payload(workspace)
    result = {
        m.id: m
        for m in await meters.build_workspace_quota_usage(
            dependencies, workspace, "user", usage, plan
        )
    }
    assert len(result) == 19  # Sleep omitted when its source payload is absent.
    assert result["contexts"].limit == workspace.effective_max_contexts
    assert result["contexts"].bonus == 3
    assert result["members"].used == 3
    assert result["storage"].used == 1234
    assert result["storage"].bonus == 10 * 1024 * 1024
    assert result["mcp_day"].limit == tier.mcp_calls_per_day + 100
    assert result["mcp_week"].limit == tier.mcp_calls_per_week + 100
    assert result["workspaces"].scope == "account"
    assert result["bound_key_rate"].scope == "api_key"
    assert result["bound_key_rate"].used is None
    meters.get_today_analysis_count.assert_awaited_once_with(
        dependencies, workspace_id=workspace.id, user_timezone="Asia/Tokyo"
    )

    queries = [
        call.args[0].compile(dialect=postgresql.dialect())
        for call in dependencies.execute.await_args_list
    ]
    for query in queries:
        assert workspace.id in query.params.values()
    assert "deleted_at IS NULL" in str(queries[0])
    assert "accepted_at IS NULL" in str(queries[0])
    assert "expires_at IS NULL" in str(queries[0])
    assert "workspace_connectors.id IS NULL" in str(queries[1])
    assert "resource_tokens.is_active" in str(queries[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(("plan_name", "creates"), [("basic", False), ("promax", True)])
async def test_xl_only_meters_say_whether_creation_is_included(dependencies, plan_name, creates):
    """Below XL the connector / resource caps only serve existing objects (#1551)."""
    workspace = Workspace(id=uuid4(), plan_name=plan_name)
    plan, usage, tier = payload(workspace)
    result = {
        m.id: m
        for m in await meters.build_workspace_quota_usage(
            dependencies, workspace, "user", usage, plan
        )
    }
    for meter_id in ("connectors", "resource_tokens", "resource_capacity"):
        assert result[meter_id].creation_included is creates
    # The retained-object cap stays visible either way.
    assert result["connectors"].limit == tier.max_connectors > 0
    assert result["memories"].creation_included is True


@pytest.mark.asyncio
async def test_ungated_context_features_have_no_ceiling(dependencies, monkeypatch):
    workspace = Workspace(id=uuid4(), plan_name="free", addon_context_bonus=9)
    monkeypatch.setattr(meters, "content_chunks_gated", lambda: False)
    monkeypatch.setattr(meters, "llm_selector_gated", lambda: False)
    plan, usage, _ = payload(workspace)
    result = {
        m.id: m
        for m in await meters.build_workspace_quota_usage(
            dependencies, workspace, "user", usage, plan
        )
    }
    assert result["content_chunks"].limit is None
    assert result["llm_selector"].limit is None
    assert result["rest_day"].limit == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
async def test_cost_meters_require_workspace_admin(dependencies, monkeypatch, role):
    from services.embedding_spend_cap_service import EmbeddingSpendCapService
    from utils.exceptions import AuthorizationError

    workspace = Workspace(id=uuid4(), plan_name="pro")
    monkeypatch.setattr(
        meters,
        "get_settings",
        lambda: SimpleNamespace(enable_byok=True, enable_cost_display=True),
    )
    check = AsyncMock()
    if role in ("member", "viewer"):
        check.side_effect = AuthorizationError("Insufficient permissions")
    monkeypatch.setattr(
        meters, "PermissionService", lambda db: SimpleNamespace(check_workspace_admin=check)
    )
    spend = AsyncMock(return_value=(0.12, 1.34))
    monkeypatch.setattr(EmbeddingSpendCapService, "get_current_spend", spend)
    plan, usage, _ = payload(workspace)
    result = {
        m.id: m
        for m in await meters.build_workspace_quota_usage(
            dependencies, workspace, "user", usage, plan
        )
    }
    check.assert_awaited_once_with("user", workspace.id)
    assert "storage" in result
    if role in ("owner", "admin"):
        spend.assert_awaited_once_with(workspace.id)
        assert result["embedding_day"].used == 0.12
        assert result["embedding_month"].used == 1.34
    else:
        spend.assert_not_awaited()
        assert not any(m.unit == "usd" for m in result.values())


@pytest.mark.asyncio
async def test_zero_storage_limit_means_no_enforced_ceiling(dependencies, monkeypatch):
    monkeypatch.setattr(Workspace, "effective_storage_limit_bytes", property(lambda self: 0))
    workspace = Workspace(id=uuid4(), plan_name="pro")
    plan, usage, _ = payload(workspace)
    result = {
        m.id: m
        for m in await meters.build_workspace_quota_usage(
            dependencies, workspace, "user", usage, plan
        )
    }
    assert result["storage"].limit is None
    assert result["storage"].used == 1234
