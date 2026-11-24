"""``live_public_endpoint_context_ids`` on PostgreSQL (#2995).

A context counts while it has a ``public_mcp_endpoints`` row with
``revoked_at IS NULL``; an unpublished (revoked) row and a context without a
row do not. An empty id list runs no statement.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete

from models.auth import Context, User, Workspace
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from services.sync_managed_guard import live_public_endpoint_context_ids
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_only_contexts_with_a_live_endpoint(db_session) -> None:
    tag = uuid4().hex[:8]
    owner = f"plo-{tag}"
    db_session.add(User(email=f"{owner}@example.test", user_id=owner, name=owner, role="user"))
    await db_session.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"Example {tag}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    db_session.add(ws)
    await db_session.flush()
    contexts = {}
    resources = {}
    for key in ("live", "revoked", "none"):
        slug = f"{key}-{tag}"
        resources[key] = Resource(workspace_id=ws.id, resource_id=slug, created_by=owner)
        contexts[key] = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=slug,
            created_by=owner,
            is_private=False,
            is_public=True,
            resource_id=slug,
        )
    db_session.add_all([*resources.values(), *contexts.values()])
    await db_session.flush()
    for key, revoked in (("live", None), ("revoked", utcnow())):
        db_session.add(
            PublicMcpEndpoint(
                public_id="pmcp_" + uuid4().hex[:22],
                workspace_id=ws.id,
                context_id=contexts[key].id,
                resource_pk=resources[key].id,
                resource_id=resources[key].resource_id,
                public_name="Example Shop",
                created_by=owner,
                revoked_at=revoked,
            )
        )
    await db_session.commit()
    try:
        ids = [c.id for c in contexts.values()]
        assert await live_public_endpoint_context_ids(db_session, ids) == {contexts["live"].id}
        assert await live_public_endpoint_context_ids(db_session, []) == set()
    finally:
        await db_session.execute(
            delete(PublicMcpEndpoint).where(PublicMcpEndpoint.workspace_id == ws.id)
        )
        await db_session.execute(delete(Context).where(Context.workspace_id == ws.id))
        await db_session.execute(delete(Resource).where(Resource.workspace_id == ws.id))
        await db_session.execute(delete(Workspace).where(Workspace.id == ws.id))
        await db_session.execute(delete(User).where(User.user_id == owner))
        await db_session.commit()
