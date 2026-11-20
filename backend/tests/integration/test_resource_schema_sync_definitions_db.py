"""The schema route serves a sync-created schema (#2951).

Sync provisioning stores ``models.sync_feed_schemas.DEFINITIONS`` as the
catalog schema. Those field definitions carry no ``description`` (and must
not gain one: the feed checks the stored definitions against ``SCHEMA_HASH``),
so the response model may not require it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session, require_workspace_owner
from auth.workspace_roles import WorkspaceRole
from db.base import get_db
from models.auth import Context, Workspace, WorkspaceMember
from models.resource import Resource, ResourceSchema
from models.sync_feed_schemas import DEFINITIONS, SCHEMA_HASH
from models.management_schemas import digest


def _session_override(engine):
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with maker() as session:
            yield session

    return override_get_db


@pytest.mark.asyncio
async def test_sync_created_schema_is_served(async_engine, db_session):
    owner = f"owner_{uuid4().hex[:8]}"
    slug = f"sync-{uuid4().hex}"
    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    resource = Resource(
        id=uuid4(), workspace_id=ws.id, resource_id=slug, name="sync", created_by=owner
    )
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        resource_id=slug,
        created_by=owner,
    )
    schema = ResourceSchema(
        resource_pk=resource.id, resource_id=slug, schema_version=1, field_definitions=DEFINITIONS
    )
    db_session.add(ws)
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER),
            resource,
            ctx,
            schema,
        ]
    )
    await db_session.commit()

    async def user():
        return {
            "user_id": owner,
            "email": f"{owner}@test.com",
            "role": "user",
            "current_workspace_id": ws.id,
            "workspace_role": "owner",
        }

    async def owner_gate():
        return (owner, ws.id)

    app.dependency_overrides[get_db] = _session_override(async_engine)
    app.dependency_overrides[get_user_from_api_key_or_session] = user
    app.dependency_overrides[require_workspace_owner] = owner_gate
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(f"/api/v1/resources/{slug}/schema")
        assert response.status_code == 200, response.text
        fields = response.json()["field_definitions"]
        assert [f["name"] for f in fields] == [d["name"] for d in DEFINITIONS]
        assert all(f["description"] == "" for f in fields)
        # The stored definitions are untouched: the feed's hash check still holds.
        await db_session.refresh(schema)
        assert digest(schema.field_definitions) == SCHEMA_HASH
    finally:
        app.dependency_overrides.clear()
        await db_session.delete(schema)
        await db_session.delete(ctx)
        await db_session.delete(resource)
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id == ws.id)
        )
        await db_session.delete(ws)
        await db_session.commit()
