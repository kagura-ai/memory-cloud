"""Quota dashboard counts share tenant, deletion, invitation and token boundaries."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api.routes import workspace_quota_usage as meters
from api.routes.usage import CurrentUsage, PlanLimits, WorkspacesUsage
from models.auth import WorkspaceInvitation, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.resource import Resource, ResourceToken, WorkspaceConnector
from utils.datetime import utcnow

from ._admin_helpers import make_context, make_user, make_workspace


@pytest.mark.asyncio
async def test_quota_counts_stay_in_workspace_and_use_live_populations(db_session, monkeypatch):
    owner = make_user()
    db_session.add(owner)
    await db_session.flush()
    workspace = make_workspace(owner_user_id=owner.user_id)
    other = make_workspace(owner_user_id=owner.user_id)
    db_session.add_all([workspace, other])
    await db_session.flush()
    live = make_context(workspace_id=workspace.id, created_by=owner.user_id)
    deleted = make_context(workspace_id=workspace.id, created_by=owner.user_id)
    deleted.deleted_at = utcnow()
    foreign = make_context(workspace_id=other.id, created_by=owner.user_id)
    db_session.add_all([live, deleted, foreign])
    await db_session.flush()
    for context in [live, deleted, foreign]:
        db_session.add(
            ContextSearchConfig(
                context_id=context.id, content_chunks_enabled=True, llm_selector_enabled=True
            )
        )
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=owner.user_id, role=WorkspaceRole.OWNER)
    )
    now = utcnow()
    for ws, expires, accepted in [
        (workspace, None, None),
        (workspace, now + timedelta(days=1), None),
        (workspace, now - timedelta(days=1), None),
        (workspace, None, now),
        (other, None, None),
    ]:
        db_session.add(
            WorkspaceInvitation(
                workspace_id=ws.id,
                token=uuid4().hex,
                invited_by=owner.user_id,
                expires_at=expires,
                accepted_at=accepted,
            )
        )
    for ws, active, connector in [
        (workspace, True, False),
        (workspace, False, False),
        (workspace, True, True),
        (other, True, False),
    ]:
        resource = Resource(id=uuid4(), workspace_id=ws.id, resource_id="resource-" + uuid4().hex)
        db_session.add(resource)
        await db_session.flush()
        db_session.add(
            ResourceToken(
                resource_pk=resource.id,
                workspace_id=ws.id,
                resource_id=resource.resource_id,
                token_hash=uuid4().hex,
                is_active=active,
                quota_events_per_hour=700,
            )
        )
        if connector:
            db_session.add(
                WorkspaceConnector(
                    resource_pk=resource.id, workspace_id=ws.id, connector_type="slack"
                )
            )
    await db_session.flush()

    monkeypatch.setattr(meters, "get_current_storage_usage", AsyncMock(return_value=123))
    plan = PlanLimits(
        plan_name="pro",
        memory_limit=workspace.effective_memory_limit,
        daily_total_limit=0,
        weekly_total_limit=0,
    )
    usage = CurrentUsage(
        memory_count=0,
        api_calls_today=0,
        api_calls_this_week=0,
        workspaces=WorkspacesUsage(used=2, limit=3, remaining=1),
    )
    result = {
        m.id: m
        for m in await meters.build_workspace_quota_usage(
            db_session, workspace, owner.user_id, usage, plan
        )
    }
    assert result["contexts"].used == 1
    assert result["content_chunks"].used == 1
    assert result["llm_selector"].used == 1
    assert result["members"].used == 3  # Owner plus two unexpired invitations.
    assert result["connectors"].used == 1
    assert result["resource_tokens"].used == 1
    assert result["resource_capacity"].used == 700
    assert result["analysis"].used == 0


@pytest.mark.asyncio
async def test_seat_suspended_admin_reads_usage_without_cost_meters(db_session, monkeypatch):
    """A free plan seats only the owner: the admin's seat is suspended.

    The cost-meter role check refuses that admin with ``FEAT-001``; the
    endpoint still answers 200 with the non-monetary meters, as before the
    quota meters existed.
    """
    from httpx import ASGITransport, AsyncClient

    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db

    owner, admin = make_user(), make_user()
    db_session.add_all([owner, admin])
    await db_session.flush()
    workspace = make_workspace(owner_user_id=owner.user_id, plan_name="free")
    db_session.add(workspace)
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=workspace.id, user_id=owner.user_id, role=WorkspaceRole.OWNER
            ),
            WorkspaceMember(
                workspace_id=workspace.id, user_id=admin.user_id, role=WorkspaceRole.ADMIN
            ),
        ]
    )
    await db_session.flush()
    monkeypatch.setattr(
        meters,
        "get_settings",
        lambda: SimpleNamespace(enable_byok=True, enable_cost_display=True),
    )

    async def _db():
        yield db_session

    async def _user() -> dict:
        return {"user_id": admin.user_id, "current_workspace_id": workspace.id}

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_user_from_api_key_or_session] = _user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/workspace/usage/current")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_user_from_api_key_or_session, None)

    assert response.status_code == 200, response.text
    quota = response.json()["quota_usage"]
    assert any(m["id"] == "storage" for m in quota)
    assert not any(m["unit"] == "usd" for m in quota)
