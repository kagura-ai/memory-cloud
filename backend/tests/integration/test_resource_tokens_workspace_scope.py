"""Resource-token write paths stay inside one workspace's resource (#1877).

A resource slug is only unique among *live* contexts, so once workspace A
soft-deletes its context the slug can be taken by workspace B while A's
tokens are still active (#1863 keeps them listed and revocable). The
auto-revoke on "context deleted" / "context re-slugged" was keyed on the
bare slug and deactivated A's leftover tokens when B deleted or re-slugged
its own context. MCP ``list_resource_tokens`` hid A's tokens behind a
live-context filter, and the quota ceiling on PATCH was summed over the
caller instead of the workspace.

Real-DB tests (same fixture shape as
``test_resource_tokens_after_context_delete.py``): the tenant boundary is a
property of the SQL, which a mocked session cannot prove.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import config.plan_tiers as plan_tiers_module
from api.main import app
from auth.dependencies import (
    get_user_from_api_key_or_session,
    require_session_auth,
    require_workspace_owner,
)
from auth.resource_tokens import ResourceTokenManager, workspace_regular_active_tokens
from auth.workspace_roles import WorkspaceRole
from config.plan_tiers import get_plan_tier
from db.base import get_db
from mcp_server.tools.context import handle_update_context
from mcp_server.tools.resource import handle_list_resource_tokens, handle_setup_resource
from models.auth import Context, Workspace, WorkspaceMember
from models.resource import Resource, ResourceSchema, ResourceToken, WorkspaceConnector
from services.context_service import ContextService
from utils.datetime import utcnow
from utils.exceptions import QuotaExceededError

PLAN = "basic"


def _session_maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _fresh_session_override(engine):
    session_maker = _session_maker(engine)

    async def override_get_db():
        async with session_maker() as session:
            try:
                yield session
            finally:
                await session.rollback()

    return override_get_db


@pytest_asyncio.fixture
async def scenario(async_engine, db_session):
    """Two workspaces holding the SAME slug.

    Workspace A's context is already soft-deleted and its token is still
    active (the #1863 state). Workspace B has taken the slug with a live
    context and holds two active tokens: one minted by its owner, one by
    another member.
    """
    tag = uuid4().hex[:8]
    owner_a_id = f"owner_a_{tag}"
    owner_b_id = f"owner_b_{tag}"
    member_b_id = f"member_b_{tag}"
    slug = f"orders_{tag}"

    ws_a_id, ws_b_id = uuid4(), uuid4()
    resource_a_id, resource_b_id = uuid4(), uuid4()
    ctx_a_id, ctx_b_id = uuid4(), uuid4()

    db_session.add_all(
        [
            Workspace(
                id=ws_a_id,
                name=f"ws-a-{tag}",
                plan_name=PLAN,
                owner_user_id=owner_a_id,
                daily_api_limit=50000,
                weekly_api_limit=250000,
            ),
            Workspace(
                id=ws_b_id,
                name=f"ws-b-{tag}",
                plan_name=PLAN,
                owner_user_id=owner_b_id,
                daily_api_limit=50000,
                weekly_api_limit=250000,
            ),
        ]
    )
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws_a_id, user_id=owner_a_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=ws_b_id, user_id=owner_b_id, role=WorkspaceRole.OWNER),
            Resource(
                id=resource_a_id, workspace_id=ws_a_id, resource_id=slug, created_by=owner_a_id
            ),
            Resource(
                id=resource_b_id, workspace_id=ws_b_id, resource_id=slug, created_by=owner_b_id
            ),
            # Soft-deleted from the start: the slug is free for workspace B.
            Context(
                id=ctx_a_id,
                workspace_id=ws_a_id,
                name=f"ctx-a-{tag}",
                resource_id=slug,
                created_by=owner_a_id,
                deleted_at=utcnow(),
            ),
            Context(
                id=ctx_b_id,
                workspace_id=ws_b_id,
                name=f"ctx-b-{tag}",
                resource_id=slug,
                is_private=False,
                is_public=True,
                created_by=owner_b_id,
            ),
        ]
    )
    await db_session.flush()

    manager = ResourceTokenManager(db_session)
    _, token_a = await manager.create_token(
        slug, resource_pk=resource_a_id, workspace_id=ws_a_id, created_by=owner_a_id
    )
    _, token_b = await manager.create_token(
        slug, resource_pk=resource_b_id, workspace_id=ws_b_id, created_by=owner_b_id
    )
    _, token_b_member = await manager.create_token(
        slug, resource_pk=resource_b_id, workspace_id=ws_b_id, created_by=member_b_id
    )
    ids = {
        "token_a_id": token_a.id,
        "token_a_public_id": token_a.public_id,
        "token_b_id": token_b.id,
        "token_b_public_id": token_b.public_id,
        "token_b_member_id": token_b_member.id,
        "token_b_member_public_id": token_b_member.public_id,
    }
    await db_session.commit()

    def act_as(user_id, workspace_id):
        async def override_auth():
            return {
                "user_id": user_id,
                "email": f"{user_id}@test.com",
                "role": "user",
                "current_workspace_id": workspace_id,
                "workspace_role": "owner",
            }

        async def override_owner():
            return (user_id, workspace_id)

        app.dependency_overrides[get_db] = _fresh_session_override(async_engine)
        app.dependency_overrides[get_user_from_api_key_or_session] = override_auth
        app.dependency_overrides[require_session_auth] = override_auth
        app.dependency_overrides[require_workspace_owner] = override_owner

    yield {
        "slug": slug,
        "tag": tag,
        "ws_a_id": ws_a_id,
        "ws_b_id": ws_b_id,
        "owner_a_id": owner_a_id,
        "owner_b_id": owner_b_id,
        "member_b_id": member_b_id,
        "resource_a_id": resource_a_id,
        "resource_b_id": resource_b_id,
        "ctx_b_id": ctx_b_id,
        "act_as": act_as,
        "engine": async_engine,
        **ids,
    }

    app.dependency_overrides.clear()
    ws_ids = [ws_a_id, ws_b_id]
    try:
        await db_session.rollback()
        await db_session.execute(
            ResourceToken.__table__.delete().where(ResourceToken.workspace_id.in_(ws_ids))
        )
        await db_session.execute(
            WorkspaceConnector.__table__.delete().where(WorkspaceConnector.workspace_id.in_(ws_ids))
        )
        await db_session.execute(
            ResourceSchema.__table__.delete().where(
                ResourceSchema.resource_pk.in_(
                    select(Resource.id).where(Resource.workspace_id.in_(ws_ids))
                )
            )
        )
        await db_session.execute(Context.__table__.delete().where(Context.workspace_id.in_(ws_ids)))
        await db_session.execute(
            Resource.__table__.delete().where(Resource.workspace_id.in_(ws_ids))
        )
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id.in_(ws_ids))
        )
        await db_session.execute(Workspace.__table__.delete().where(Workspace.id.in_(ws_ids)))
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


async def _is_active(db_session, token_id: int) -> bool:
    db_session.expire_all()
    result = await db_session.execute(
        select(ResourceToken.is_active).where(ResourceToken.id == token_id)
    )
    return result.scalar_one()


def _mcp_db(engine):
    """``db.base.get_db`` stand-in for MCP handlers, bound to the test engine."""
    session_maker = _session_maker(engine)

    async def get_db_override():
        async with session_maker() as session:
            yield session

    return get_db_override


def _json_of(result):
    return json.loads(result[0].text)


# ============================================================================
# Auto-revoke on context delete / resource_id change
# ============================================================================


@pytest.mark.asyncio
async def test_revoke_for_resource_leaves_the_same_slug_of_another_workspace(scenario, db_session):
    manager = ResourceTokenManager(db_session)
    revoked = await manager.revoke_tokens_for_resource(scenario["ws_b_id"], scenario["slug"])
    await db_session.commit()

    assert revoked == 2
    assert await _is_active(db_session, scenario["token_b_id"]) is False
    assert await _is_active(db_session, scenario["token_b_member_id"]) is False
    assert await _is_active(db_session, scenario["token_a_id"]) is True


@pytest.mark.asyncio
async def test_revoke_for_resource_covers_rows_missing_a_shadow_column(scenario, db_session):
    """A token without ``workspace_id`` is still reached through its
    ``resource_pk``; a legacy token without ``resource_pk`` through slug +
    ``workspace_id``. Neither path crosses into the other workspace."""
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_id"])
        .values(workspace_id=None)
    )
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_member_id"])
        .values(resource_pk=None)
    )
    # Workspace A's token as a legacy row too: same slug, other workspace.
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_a_id"])
        .values(resource_pk=None)
    )
    await db_session.commit()

    manager = ResourceTokenManager(db_session)
    revoked = await manager.revoke_tokens_for_resource(scenario["ws_b_id"], scenario["slug"])
    await db_session.commit()

    assert revoked == 2
    assert await _is_active(db_session, scenario["token_a_id"]) is True


@pytest.mark.asyncio
async def test_rest_delete_context_keeps_the_other_workspaces_token(scenario, db_session):
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])

    async def soft_delete(self, user_id, context_id, **_kwargs):
        # The real service also removes the vector collection; the token
        # revocation under test happens in the route, in the same transaction.
        await self.db.execute(
            Context.__table__.update().where(Context.id == context_id).values(deleted_at=utcnow())
        )
        await self.db.commit()

    with patch.object(ContextService, "delete_context", new=soft_delete):
        with TestClient(app) as client:
            response = client.delete(f"/api/v1/contexts/{scenario['ctx_b_id']}")
    assert response.status_code == 204, response.text

    # Workspace B's own tokens go, whoever minted them …
    assert await _is_active(db_session, scenario["token_b_id"]) is False
    assert await _is_active(db_session, scenario["token_b_member_id"]) is False
    # … workspace A's leftover token of the same slug does not.
    assert await _is_active(db_session, scenario["token_a_id"]) is True


@pytest.mark.asyncio
async def test_rest_reslug_keeps_the_other_workspaces_token(scenario, db_session):
    # Same minter on both sides, so ``created_by`` cannot be what separates
    # the two workspaces.
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_a_id"])
        .values(created_by=scenario["owner_b_id"])
    )
    await db_session.commit()
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])

    with TestClient(app) as client:
        response = client.put(
            f"/api/v1/contexts/{scenario['ctx_b_id']}",
            json={"resource_id": f"renamed_{scenario['tag']}"},
        )
    assert response.status_code == 200, response.text

    assert await _is_active(db_session, scenario["token_b_id"]) is False
    # Unchanged rule: only the caller's own tokens are revoked on a re-slug.
    assert await _is_active(db_session, scenario["token_b_member_id"]) is True
    assert await _is_active(db_session, scenario["token_a_id"]) is True


@pytest.mark.asyncio
async def test_mcp_reslug_keeps_the_other_workspaces_token(scenario, db_session):
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_a_id"])
        .values(created_by=scenario["owner_b_id"])
    )
    await db_session.commit()

    with (
        patch("db.base.get_db", new=_mcp_db(scenario["engine"])),
        patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
    ):
        result = await handle_update_context(
            {
                "context_id": str(scenario["ctx_b_id"]),
                "resource_id": f"renamed_{scenario['tag']}",
            },
            scenario["owner_b_id"],
            scenario["ws_b_id"],
        )
    assert _json_of(result)["status"] == "success", result[0].text

    assert await _is_active(db_session, scenario["token_b_id"]) is False
    assert await _is_active(db_session, scenario["token_b_member_id"]) is True
    assert await _is_active(db_session, scenario["token_a_id"]) is True


# ============================================================================
# MCP list_resource_tokens
# ============================================================================


async def _mcp_list(scenario, args):
    with (
        patch("db.base.get_db", new=_mcp_db(scenario["engine"])),
        patch("mcp_server.tools.resource._log_tool_usage", new=AsyncMock()),
    ):
        return _json_of(
            await handle_list_resource_tokens(args, scenario["owner_a_id"], scenario["ws_a_id"])
        )


@pytest.mark.asyncio
async def test_mcp_list_shows_a_token_whose_context_was_deleted(scenario):
    """Workspace A has no live context left; its token still authenticates
    ingest, so the audit must list it — unfiltered and by slug — and must not
    pick up workspace B's tokens of the same slug."""
    unfiltered = await _mcp_list(scenario, {})
    assert unfiltered["status"] == "success", unfiltered
    assert [t["id"] for t in unfiltered["tokens"]] == [scenario["token_a_public_id"]]
    assert unfiltered["total"] == 1

    filtered = await _mcp_list(scenario, {"resource_id": scenario["slug"]})
    assert filtered["status"] == "success", filtered
    assert [t["id"] for t in filtered["tokens"]] == [scenario["token_a_public_id"]]
    assert filtered["total"] == 1
    assert filtered["tokens"][0]["is_active"] is True


@pytest.mark.asyncio
async def test_mcp_list_answers_resource_not_found_for_a_foreign_slug(scenario, db_session):
    foreign_slug = f"only_b_{scenario['tag']}"
    foreign_pk = uuid4()
    db_session.add(
        Resource(
            id=foreign_pk,
            workspace_id=scenario["ws_b_id"],
            resource_id=foreign_slug,
            created_by=scenario["owner_b_id"],
        )
    )
    await db_session.flush()
    await ResourceTokenManager(db_session).create_token(
        foreign_slug,
        resource_pk=foreign_pk,
        workspace_id=scenario["ws_b_id"],
        created_by=scenario["owner_b_id"],
    )
    await db_session.commit()

    data = await _mcp_list(scenario, {"resource_id": foreign_slug})
    assert data["status"] == "error"
    assert data["error"] == "resource_not_found"


# ============================================================================
# Quota ceiling on PATCH
# ============================================================================


@pytest.mark.asyncio
async def test_quota_ceiling_is_the_workspaces_not_the_callers(scenario, db_session):
    """The owner raises the quota of a token another member minted. The
    budget is the workspace's: the owner's own token counts against it, and
    the caller's tokens in ANOTHER workspace do not."""
    ceiling = get_plan_tier(PLAN).max_resource_tokens * 10000
    # Workspace B: owner's token takes all but 1500 of the ceiling.
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_id"])
        .values(quota_events_per_hour=ceiling - 1500)
    )
    # Noise the old caller-keyed sum would have counted: a token of workspace
    # A minted by the same user.
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_a_id"])
        .values(created_by=scenario["owner_b_id"], quota_events_per_hour=10000)
    )
    await db_session.commit()
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])
    member_token = scenario["token_b_member_public_id"]

    with TestClient(app) as client:
        fits = client.patch(
            f"/api/v1/resource-tokens/{member_token}", json={"quota_events_per_hour": 1500}
        )
        assert fits.status_code == 200, fits.text
        assert fits.json()["quota_events_per_hour"] == 1500

        over = client.patch(
            f"/api/v1/resource-tokens/{member_token}", json={"quota_events_per_hour": 1501}
        )
        assert over.status_code == 400, over.text
        assert str(ceiling) in over.json()["message"]


@pytest.mark.asyncio
async def test_quota_sum_counts_a_token_without_the_shadow_workspace_id(scenario, db_session):
    """A token whose ``workspace_id`` was never backfilled still authenticates
    through ``resource_pk``, so its quota is part of the workspace's sum; a
    legacy row without ``resource_pk`` counts through its ``workspace_id``."""
    ceiling = get_plan_tier(PLAN).max_resource_tokens * 10000
    legacy_pk = uuid4()
    legacy_slug = f"legacy_{scenario['tag']}"
    db_session.add(
        Resource(
            id=legacy_pk,
            workspace_id=scenario["ws_b_id"],
            resource_id=legacy_slug,
            created_by=scenario["owner_b_id"],
        )
    )
    await db_session.flush()
    _, legacy_token = await ResourceTokenManager(db_session).create_token(
        legacy_slug,
        resource_pk=legacy_pk,
        workspace_id=scenario["ws_b_id"],
        quota_events_per_hour=1000,
        created_by=scenario["owner_b_id"],
    )
    await db_session.flush()
    # token_b: resource_pk only (no shadow workspace_id); legacy_token:
    # workspace_id only (no resource_pk). Together all but 1500 of the ceiling.
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_id"])
        .values(workspace_id=None, quota_events_per_hour=ceiling - 2500)
    )
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == legacy_token.id)
        .values(resource_pk=None)
    )
    await db_session.commit()
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])
    member_token = scenario["token_b_member_public_id"]

    with TestClient(app) as client:
        fits = client.patch(
            f"/api/v1/resource-tokens/{member_token}", json={"quota_events_per_hour": 1500}
        )
        assert fits.status_code == 200, fits.text
        over = client.patch(
            f"/api/v1/resource-tokens/{member_token}", json={"quota_events_per_hour": 1501}
        )
        assert over.status_code == 400, over.text


@pytest.mark.asyncio
async def test_lowering_a_quota_is_never_refused(scenario, db_session):
    """Already over the ceiling (a plan downgrade, say): lowering must work —
    it is the way back under."""
    ceiling = get_plan_tier(PLAN).max_resource_tokens * 10000
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_id"])
        .values(quota_events_per_hour=ceiling + 5000)
    )
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_member_id"])
        .values(quota_events_per_hour=9000)
    )
    await db_session.commit()
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])
    member_token = scenario["token_b_member_public_id"]

    with TestClient(app) as client:
        lowered = client.patch(
            f"/api/v1/resource-tokens/{member_token}", json={"quota_events_per_hour": 8000}
        )
        assert lowered.status_code == 200, lowered.text
        assert lowered.json()["quota_events_per_hour"] == 8000

        unchanged = client.patch(
            f"/api/v1/resource-tokens/{member_token}", json={"quota_events_per_hour": 8000}
        )
        assert unchanged.status_code == 200, unchanged.text

        raised = client.patch(
            f"/api/v1/resource-tokens/{member_token}", json={"quota_events_per_hour": 8001}
        )
        assert raised.status_code == 400, raised.text


@pytest.mark.asyncio
async def test_connector_owned_tokens_are_outside_the_budget(scenario, db_session):
    """Consistent with the create-time cap (#858): a connector's token does
    not count toward the workspace's sum."""
    ceiling = get_plan_tier(PLAN).max_resource_tokens * 10000
    connector_slug = f"conn_{scenario['tag']}"
    connector_pk = uuid4()
    db_session.add(
        Resource(
            id=connector_pk,
            workspace_id=scenario["ws_b_id"],
            resource_id=connector_slug,
            created_by=scenario["owner_b_id"],
        )
    )
    await db_session.flush()
    db_session.add(
        WorkspaceConnector(
            workspace_id=scenario["ws_b_id"],
            resource_pk=connector_pk,
            connector_type="slack",
        )
    )
    _, connector_token = await ResourceTokenManager(db_session).create_token(
        connector_slug,
        resource_pk=connector_pk,
        workspace_id=scenario["ws_b_id"],
        quota_events_per_hour=10000,
        created_by=scenario["owner_b_id"],
    )
    connector_public_id = connector_token.public_id
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_id"])
        .values(quota_events_per_hour=ceiling - 1500)
    )
    await db_session.commit()
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])

    with TestClient(app) as client:
        # The connector's 10000 is not in the sum: 1500 still fits.
        fits = client.patch(
            f"/api/v1/resource-tokens/{scenario['token_b_member_public_id']}",
            json={"quota_events_per_hour": 1500},
        )
        assert fits.status_code == 200, fits.text

        # And the connector's own token is not measured against that budget.
        connector = client.patch(
            f"/api/v1/resource-tokens/{connector_public_id}",
            json={"quota_events_per_hour": 9000},
        )
        assert connector.status_code == 200, connector.text
        connector_up = client.patch(
            f"/api/v1/resource-tokens/{connector_public_id}",
            json={"quota_events_per_hour": 10000},
        )
        assert connector_up.status_code == 200, connector_up.text


# ============================================================================
# Token cap and quota ceiling share one population (#1919)
# ============================================================================


async def _set_quota(db_session, token_id: int, quota: int) -> None:
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == token_id)
        .values(quota_events_per_hour=quota)
    )


@pytest.fixture
def xl_with_cap(monkeypatch):
    """Make workspace B an XL workspace (the only tier that may mint) whose
    ``max_resource_tokens`` is ``cap`` — the registry value (150) would need
    150 tokens to reach."""

    def _apply(cap: int) -> int:
        tier = dataclasses.replace(plan_tiers_module.PLAN_TIERS["promax"], max_resource_tokens=cap)
        monkeypatch.setitem(plan_tiers_module.PLAN_TIERS, "promax", tier)
        return cap * 10000

    return _apply


async def _make_xl(db_session, workspace_id) -> None:
    await db_session.execute(
        Workspace.__table__.update().where(Workspace.id == workspace_id).values(plan_name="promax")
    )


def _mint(client, slug: str, quota: int):
    return client.post(
        "/api/v1/resource-tokens",
        json={"resource_id": slug, "quota_events_per_hour": quota},
    )


@pytest.mark.asyncio
async def test_two_owners_cannot_outrun_the_ceiling_through_creation(
    scenario, db_session, xl_with_cap
):
    """Two owners each hold one token near the per-token maximum, and the
    workspace is at its token cap. The cap used to be counted per creator, so
    the second owner could mint a third token and push the workspace's sum
    over ``max_resource_tokens * 10000`` — after which every quota raise, by
    anyone, answered 400. The cap is the workspace's: the third mint is
    refused by whoever asks, and a raise that fits the ceiling still works."""
    ceiling = xl_with_cap(2)
    await _make_xl(db_session, scenario["ws_b_id"])
    # The second minter is an owner too (membership is what makes them one;
    # the REST gate is overridden below like every other test here).
    db_session.add(
        WorkspaceMember(
            workspace_id=scenario["ws_b_id"],
            user_id=scenario["member_b_id"],
            role=WorkspaceRole.OWNER,
        )
    )
    await _set_quota(db_session, scenario["token_b_id"], 9000)
    await _set_quota(db_session, scenario["token_b_member_id"], 9000)
    await db_session.commit()

    for minter in (scenario["member_b_id"], scenario["owner_b_id"]):
        scenario["act_as"](minter, scenario["ws_b_id"])
        with TestClient(app) as client:
            refused = _mint(client, scenario["slug"], 10000)
        assert refused.status_code == 403, refused.text
        body = refused.json()
        assert body["error"] == "QUOTA-001"
        assert (body["details"]["current"], body["details"]["limit"]) == (2, 2)

    # Creation alone can never exceed the ceiling: count <= cap and every
    # token <= 10000 keep the sum at or under cap * 10000, so every token can
    # still be raised to the per-token maximum (the sum lands exactly on the
    # ceiling, which fits).
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])
    with TestClient(app) as client:
        for public_id in (scenario["token_b_member_public_id"], scenario["token_b_public_id"]):
            raised = client.patch(
                f"/api/v1/resource-tokens/{public_id}", json={"quota_events_per_hour": 10000}
            )
            assert raised.status_code == 200, raised.text
    db_session.expire_all()
    total = await db_session.scalar(
        select(func.sum(ResourceToken.quota_events_per_hour)).where(
            ResourceToken.workspace_id == scenario["ws_b_id"], ResourceToken.is_active.is_(True)
        )
    )
    assert total == ceiling


@pytest.mark.asyncio
async def test_cap_still_bites_at_max_for_a_single_owner(scenario, db_session, xl_with_cap):
    """Unchanged behaviour for the common shape: one owner, ``cap`` tokens."""
    xl_with_cap(3)
    await _make_xl(db_session, scenario["ws_b_id"])
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_member_id"])
        .values(created_by=scenario["owner_b_id"])
    )
    await db_session.commit()
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])

    with TestClient(app) as client:
        third = _mint(client, scenario["slug"], 1000)
        assert third.status_code == 201, third.text
        fourth = _mint(client, scenario["slug"], 1000)
    assert fourth.status_code == 403, fourth.text
    assert (fourth.json()["details"]["current"], fourth.json()["details"]["limit"]) == (3, 3)


@pytest.mark.asyncio
async def test_cap_counts_like_the_ceiling_and_not_across_workspaces(
    scenario, db_session, xl_with_cap
):
    """The count's population is the ceiling's: a token reached only through
    its ``resource_pk`` (no shadow ``workspace_id``) and a legacy row reached
    only through its ``workspace_id`` both count; the same slug's token in
    workspace A, minted by the same user, does not."""
    xl_with_cap(2)
    await _make_xl(db_session, scenario["ws_b_id"])
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_id"])
        .values(workspace_id=None)
    )
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_member_id"])
        .values(resource_pk=None)
    )
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_a_id"])
        .values(created_by=scenario["owner_b_id"])
    )
    await db_session.commit()
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])

    with TestClient(app) as client:
        refused = _mint(client, scenario["slug"], 1000)
    assert refused.status_code == 403, refused.text
    assert refused.json()["details"]["current"] == 2

    # Revoke the legacy row: one slot frees up, workspace A's token is not
    # what fills it.
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_member_id"])
        .values(is_active=False)
    )
    await db_session.commit()
    with TestClient(app) as client:
        minted = _mint(client, scenario["slug"], 1000)
    assert minted.status_code == 201, minted.text


@pytest.mark.asyncio
async def test_a_token_counted_by_the_ceiling_is_addressable(scenario, db_session):
    """A token whose shadow ``workspace_id`` was never backfilled is part of
    the workspace's sum (through its ``resource_pk``) — so the owner must be
    able to list, update and revoke it through the same routes. It used to be
    missing from the list and a 404 on both writes."""
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_id"])
        .values(workspace_id=None)
    )
    await db_session.commit()
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])
    public_id = scenario["token_b_public_id"]

    with TestClient(app) as client:
        for params in ({}, {"resource_id": scenario["slug"]}):
            listed = client.get("/api/v1/resource-tokens", params=params)
            assert listed.status_code == 200, listed.text
            assert listed.json()["total"] == 2
            assert sorted(t["id"] for t in listed.json()["tokens"]) == sorted(
                [public_id, scenario["token_b_member_public_id"]]
            )
        renamed = client.patch(
            f"/api/v1/resource-tokens/{public_id}", json={"description": "reached"}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["description"] == "reached"
        raised = client.patch(
            f"/api/v1/resource-tokens/{public_id}", json={"quota_events_per_hour": 1500}
        )
        assert raised.status_code == 200, raised.text
        revoked = client.delete(f"/api/v1/resource-tokens/{public_id}")
    assert revoked.status_code == 204, revoked.text
    assert await _is_active(db_session, scenario["token_b_id"]) is False


@pytest.mark.asyncio
async def test_update_and_revoke_follow_the_resources_row_not_the_shadow_column(
    scenario, db_session
):
    """The ``resources`` row decides, as it does for the sum: a token row that
    claims workspace B but points at workspace A's resource is not B's (not
    listed, uniform 404 on writes, nothing disclosed), and workspace A's own
    token is not reachable from B either way."""
    await db_session.execute(
        ResourceToken.__table__.update()
        .where(ResourceToken.id == scenario["token_b_member_id"])
        .values(resource_pk=scenario["resource_a_id"])
    )
    await db_session.commit()
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])

    with TestClient(app) as client:
        listed = client.get("/api/v1/resource-tokens")
        assert listed.status_code == 200, listed.text
        assert [t["id"] for t in listed.json()["tokens"]] == [scenario["token_b_public_id"]]
        for public_id in (scenario["token_b_member_public_id"], scenario["token_a_public_id"]):
            patched = client.patch(
                f"/api/v1/resource-tokens/{public_id}", json={"description": "x"}
            )
            assert patched.status_code == 404, patched.text
            deleted = client.delete(f"/api/v1/resource-tokens/{public_id}")
            assert deleted.status_code == 404, deleted.text
    assert await _is_active(db_session, scenario["token_b_member_id"]) is True
    assert await _is_active(db_session, scenario["token_a_id"]) is True


# ============================================================================
# Schema registration on a context-less resource
# ============================================================================

_SCHEMA_FIELDS = [{"name": "title", "type": "text", "description": "Title"}]


async def _schema_versions(db_session, resource_pk) -> list[int]:
    db_session.expire_all()
    result = await db_session.execute(
        select(ResourceSchema.schema_version)
        .where(ResourceSchema.resource_pk == resource_pk)
        .order_by(ResourceSchema.schema_version)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_create_schema_is_refused_for_a_context_less_resource(scenario, db_session):
    """Workspace A's ``resources`` row is still there (it has an active
    token) but no live context: a schema version must not be appended to it."""
    slug = scenario["slug"]
    scenario["act_as"](scenario["owner_a_id"], scenario["ws_a_id"])

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/resources/{slug}/schema",
            json={"resource_id": slug, "field_definitions": _SCHEMA_FIELDS},
        )
    assert response.status_code == 409, response.text
    assert "no live context" in response.json()["message"]
    assert await _schema_versions(db_session, scenario["resource_a_id"]) == []
    # The same slug is live in workspace B: nothing was written there either.
    assert await _schema_versions(db_session, scenario["resource_b_id"]) == []


@pytest.mark.asyncio
async def test_create_schema_still_works_with_a_live_context(scenario, db_session):
    slug = scenario["slug"]
    scenario["act_as"](scenario["owner_b_id"], scenario["ws_b_id"])

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/resources/{slug}/schema",
            json={"resource_id": slug, "field_definitions": _SCHEMA_FIELDS},
        )
    assert response.status_code == 201, response.text
    assert response.json()["schema_version"] == 1
    assert await _schema_versions(db_session, scenario["resource_b_id"]) == [1]
    assert await _schema_versions(db_session, scenario["resource_a_id"]) == []


# ============================================================================
# Concurrent mints at cap - 1 (#1927)
# ============================================================================


@pytest.fixture
def slow_mint(monkeypatch):
    """Hold every mint between its cap count and its INSERT for a moment.

    Without the per-workspace lock both concurrent requests read the count in
    that window and both insert; with it the second request waits on the lock
    until the first one commits, then counts its token. The delay is what
    makes the interleaving deterministic instead of a matter of scheduling.
    """
    original = ResourceTokenManager.create_token

    async def delayed(self, *args, **kwargs):
        await asyncio.sleep(0.3)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ResourceTokenManager, "create_token", delayed)


async def _rest_mint(engine, user_id: str, workspace_id, slug: str):
    """POST /resource-tokens on its own session (= its own connection)."""
    from api.routes.resource_tokens import ResourceTokenCreate, create_resource_token

    async with _session_maker(engine)() as session:
        try:
            return await create_resource_token(
                ResourceTokenCreate(resource_id=slug, quota_events_per_hour=1000),
                (user_id, workspace_id),
                ResourceTokenManager(session),
                session,
            )
        except QuotaExceededError as exc:
            return exc
        finally:
            await session.rollback()


async def _active_regular_tokens(db_session, workspace_id) -> int:
    db_session.expire_all()
    return await db_session.scalar(
        workspace_regular_active_tokens(workspace_id, func.count(ResourceToken.id))
    )


@pytest.mark.asyncio
async def test_two_concurrent_rest_mints_at_cap_minus_one_admit_exactly_one(
    scenario, db_session, xl_with_cap, slow_mint
):
    """Two owners of one workspace mint at the same moment with one slot
    left. Both used to pass the COUNT before either INSERT landed and the
    workspace ended one token over ``max_resource_tokens``."""
    xl_with_cap(3)
    await _make_xl(db_session, scenario["ws_b_id"])
    await db_session.commit()
    assert await _active_regular_tokens(db_session, scenario["ws_b_id"]) == 2

    results = await asyncio.gather(
        *(
            _rest_mint(scenario["engine"], minter, scenario["ws_b_id"], scenario["slug"])
            for minter in (scenario["owner_b_id"], scenario["member_b_id"])
        )
    )

    refused = [r for r in results if isinstance(r, QuotaExceededError)]
    minted = [r for r in results if not isinstance(r, QuotaExceededError)]
    assert len(minted) == 1, results
    assert len(refused) == 1, results
    assert refused[0].status_code == 403
    assert refused[0].error_code == "QUOTA-001"
    assert (refused[0].details["current"], refused[0].details["limit"]) == (3, 3)
    assert await _active_regular_tokens(db_session, scenario["ws_b_id"]) == 3


@pytest.mark.asyncio
async def test_concurrent_rest_and_mcp_mints_share_the_lock(
    scenario, db_session, xl_with_cap, slow_mint
):
    """REST ``create_resource_token`` and MCP ``setup_resource`` take the same
    per-workspace lock: one of them racing the other for the last slot is
    refused too."""
    xl_with_cap(3)
    await _make_xl(db_session, scenario["ws_b_id"])
    await db_session.commit()

    with patch("db.base.get_db", new=_mcp_db(scenario["engine"])):
        rest_result, mcp_result = await asyncio.gather(
            _rest_mint(
                scenario["engine"], scenario["owner_b_id"], scenario["ws_b_id"], scenario["slug"]
            ),
            handle_setup_resource(
                {"name": f"ctx-new-{scenario['tag']}", "resource_id": f"new_{scenario['tag']}"},
                scenario["owner_b_id"],
                scenario["ws_b_id"],
            ),
        )

    mcp_body = _json_of(mcp_result)
    rest_refused = isinstance(rest_result, QuotaExceededError)
    mcp_refused = mcp_body.get("error") == "quota_exceeded"
    assert rest_refused != mcp_refused, (rest_result, mcp_body)
    if not mcp_refused:
        assert mcp_body["status"] == "success", mcp_body
    assert await _active_regular_tokens(db_session, scenario["ws_b_id"]) == 3
