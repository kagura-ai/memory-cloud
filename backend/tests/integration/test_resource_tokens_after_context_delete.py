"""Resource tokens stay reachable after their context is deleted (#1863).

Before #1863 the token list (with a ``resource_id`` filter), update and
revoke routes resolved the workspace boundary through a *live* ``contexts``
row. Soft-deleting a resource's last context therefore made its tokens
vanish from the UI and turned revoke into a 403, while ``verify_token`` —
which joins ``resources`` by ``resource_pk`` and never looks at contexts —
kept accepting them on ingest. The boundary is now the ``resources`` row,
and ``GET /api/v1/resources`` keeps listing a context-less resource while it
has active tokens so the tokens tab is still reachable.

Real-DB test (same fixture shape as ``test_resource_cross_workspace.py``):
the cross-workspace 403 from #268 must survive the change, which a mocked
session could not prove.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from auth.dependencies import (
    get_user_from_api_key_or_session,
    require_workspace_owner,
)
from auth.resource_tokens import ResourceTokenManager
from auth.workspace_roles import WorkspaceRole
from db.base import get_db
from models.auth import Context, Workspace, WorkspaceMember
from models.resource import Resource, ResourceToken
from utils.datetime import utcnow


def _fresh_session_override(engine):
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_maker() as session:
            try:
                yield session
            finally:
                await session.rollback()

    return override_get_db


@pytest_asyncio.fixture
async def scenario(async_engine, db_session):
    """Owner A with a resource, one context and one active token; a token in
    workspace B with the same ``created_by`` (the cross-workspace probe)."""
    tag = uuid4().hex[:8]
    owner_a_id = f"owner_a_{tag}"
    owner_b_id = f"owner_b_{tag}"
    slug_a = f"orders_{tag}"
    slug_b = f"ws_b_slug_{tag}"

    ws_a_id, ws_b_id = uuid4(), uuid4()
    resource_a_id, resource_b_id, ctx_a_id = uuid4(), uuid4(), uuid4()

    ws_a = Workspace(
        id=ws_a_id,
        name=f"ws-a-{tag}",
        plan_name="pro",
        owner_user_id=owner_a_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ws_b = Workspace(
        id=ws_b_id,
        name=f"ws-b-{tag}",
        plan_name="pro",
        owner_user_id=owner_b_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    db_session.add_all([ws_a, ws_b])
    await db_session.flush()

    resource_a = Resource(
        id=resource_a_id, workspace_id=ws_a_id, resource_id=slug_a, created_by=owner_a_id
    )
    resource_b = Resource(
        id=resource_b_id, workspace_id=ws_b_id, resource_id=slug_b, created_by=owner_b_id
    )
    ctx_a = Context(
        id=ctx_a_id,
        workspace_id=ws_a_id,
        name=f"ctx-a-{tag}",
        resource_id=slug_a,
        created_by=owner_a_id,
    )
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws_a_id, user_id=owner_a_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=ws_b_id, user_id=owner_b_id, role=WorkspaceRole.OWNER),
            resource_a,
            resource_b,
            ctx_a,
        ]
    )
    await db_session.flush()

    manager = ResourceTokenManager(db_session)
    _, token_a = await manager.create_token(
        slug_a, resource_pk=resource_a_id, workspace_id=ws_a_id, created_by=owner_a_id
    )
    # Same created_by so the only thing between owner A and this token is
    # the workspace boundary.
    _, token_b = await manager.create_token(
        slug_b, resource_pk=resource_b_id, workspace_id=ws_b_id, created_by=owner_a_id
    )
    token_a_public_id, token_a_id = token_a.public_id, token_a.id
    token_b_public_id = token_b.public_id
    await db_session.commit()

    async def override_auth():
        return {
            "user_id": owner_a_id,
            "email": f"{owner_a_id}@test.com",
            "role": "user",
            "current_workspace_id": ws_a_id,
            "workspace_role": "owner",
        }

    async def override_owner():
        return (owner_a_id, ws_a_id)

    app.dependency_overrides[get_db] = _fresh_session_override(async_engine)
    app.dependency_overrides[get_user_from_api_key_or_session] = override_auth
    app.dependency_overrides[require_workspace_owner] = override_owner

    yield {
        "slug_a": slug_a,
        "ctx_a_id": ctx_a_id,
        "token_a_public_id": token_a_public_id,
        "token_a_id": token_a_id,
        "token_b_public_id": token_b_public_id,
    }

    app.dependency_overrides.clear()
    try:
        await db_session.execute(
            ResourceToken.__table__.delete().where(ResourceToken.resource_id.in_([slug_a, slug_b]))
        )
        await db_session.execute(Context.__table__.delete().where(Context.id == ctx_a_id))
        await db_session.execute(
            Resource.__table__.delete().where(Resource.id.in_([resource_a_id, resource_b_id]))
        )
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(
                WorkspaceMember.workspace_id.in_([ws_a_id, ws_b_id])
            )
        )
        await db_session.execute(
            Workspace.__table__.delete().where(Workspace.id.in_([ws_a_id, ws_b_id]))
        )
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


async def _soft_delete(db_session, ctx_id) -> None:
    await db_session.execute(
        Context.__table__.update().where(Context.id == ctx_id).values(deleted_at=utcnow())
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_tokens_stay_listed_and_revocable_after_context_delete(scenario, db_session):
    slug = scenario["slug_a"]
    with TestClient(app) as client:
        before = client.get("/api/v1/resource-tokens", params={"resource_id": slug})
        assert before.status_code == 200, before.text
        assert [t["id"] for t in before.json()["tokens"]] == [scenario["token_a_public_id"]]

        await _soft_delete(db_session, scenario["ctx_a_id"])

        # The resource row survives with null context fields while the token
        # is active, so the tokens tab is still reachable from the list.
        listing = client.get("/api/v1/resources")
        assert listing.status_code == 200, listing.text
        row = next(r for r in listing.json()["resources"] if r["resource_id"] == slug)
        assert row["context_id"] is None
        assert row["context_name"] is None
        assert row["token_count"] == 1

        # Filtered list and revoke still resolve the resource (both were 403
        # before #1863).
        after = client.get("/api/v1/resource-tokens", params={"resource_id": slug})
        assert after.status_code == 200, after.text
        assert after.json()["total"] == 1

        revoked = client.delete(f"/api/v1/resource-tokens/{scenario['token_a_public_id']}")
        assert revoked.status_code == 204, revoked.text

        # Once the last token is revoked the context-less row disappears.
        listing_after = client.get("/api/v1/resources")
        assert all(r["resource_id"] != slug for r in listing_after.json()["resources"])

    db_session.expire_all()
    stored = (
        await db_session.execute(
            select(ResourceToken).where(ResourceToken.id == scenario["token_a_id"])
        )
    ).scalar_one()
    assert stored.is_active is False


@pytest.mark.asyncio
async def test_cross_workspace_revoke_is_still_rejected(scenario):
    """#268 boundary holds on the new ``resources``-row check."""
    with TestClient(app) as client:
        response = client.delete(f"/api/v1/resource-tokens/{scenario['token_b_public_id']}")
        assert response.status_code == 403, response.text
