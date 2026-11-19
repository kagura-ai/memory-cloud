"""Reads of the caller's current workspace require its membership (#2928).

These routes read the caller's current workspace and go through the shared
workspace access check first. On real PostgreSQL rows, over HTTP with real API
keys (and a browser session), and through ``execute_tool_call`` for MCP:

* ``GET /workspace/embedding-status``, ``GET /workspace/stats`` and
  ``GET /system/embedding/models`` answer a session whose stored current
  workspace is X, while its user is not a member of X, with the uniform
  refusal; a key scoped to X whose user is not a member of X does not
  authenticate (401);
* the MCP ``get_usage``, ``get_resource_impact`` and ``get_resource_schema``
  tools answer a caller whose session workspace is X, while not a member of X,
  with ``permission_denied``;
* a member's key and a member's session keep the response they get today.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from auth.api_keys import APIKeyManager
from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, Context, ExternalAPIKey, User, Workspace, WorkspaceMember
from models.memory import Memory

pytestmark = pytest.mark.asyncio(loop_scope="session")

REFUSAL = {"error": "AUTH-101", "message": "Insufficient permissions", "details": {}}
FAILED_SUMMARY = "Shared runbook row whose embedding failed"


def _memory(ws: UUID, ctx: UUID, author: str, summary: str, status: str) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=ws,
        context_id=ctx,
        user_id=author,
        summary=summary,
        content="full content",
        type="note",
        importance=0.5,
        client="pytest",
        scope="working",
        embedding_status=status,
        embedding_error="Model not available" if status == "failed" else None,
    )


def _app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    from api.main import http_exception_handler, memory_cloud_exception_handler
    from api.routes import system, workspace
    from db.base import get_db
    from utils.exceptions import MemoryCloudException

    app = FastAPI()
    app.include_router(workspace.router, prefix="/api/v1")
    app.include_router(system.router, prefix="/api/v1")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]

    @app.middleware("http")
    async def browser_session(request: Request, call_next):
        user_id = request.headers.get("x-test-session")
        request.state.user = (
            {"user_id": user_id, "sub": user_id, "email": f"{user_id}@example.test"}
            if user_id
            else None
        )
        return await call_next(request)

    async def request_db():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = request_db
    return app


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine, monkeypatch, fernet_api_key_secret):
    """Workspace X (owner, member, viewer) with a shared context holding one
    embedded and one failed memory, and an enabled OpenAI key; a one-seat
    workspace S whose member has no seat, with one failed memory; an outsider
    whose own workspace is Y, holding a key row scoped to X."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, member, outsider, viewer, seatless = (
        f"cw-{role}-{tag}" for role in ("owner", "member", "outsider", "viewer", "seatless")
    )
    users = [owner, member, outsider, viewer, seatless]
    x, y, s, shared, s_shared = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    async with factory() as db:
        db.add_all(
            [User(user_id=uid, email=f"{uid}@example.test", name=uid, role="user") for uid in users]
        )
        await db.flush()
        db.add_all(
            [
                Workspace(id=x, name=f"x-{tag}", owner_user_id=owner, plan_name="promax"),
                Workspace(id=y, name=f"y-{tag}", owner_user_id=outsider, plan_name="promax"),
                Workspace(id=s, name=f"s-{tag}", owner_user_id=owner, plan_name="free"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                WorkspaceMember(workspace_id=x, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(
                    workspace_id=x,
                    user_id=member,
                    role=WorkspaceRole.MEMBER,
                    allowed_context_ids=[shared],
                ),
                WorkspaceMember(workspace_id=y, user_id=outsider, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=x, user_id=viewer, role=WorkspaceRole.VIEWER),
                # One seat: the owner holds it, the member has none.
                WorkspaceMember(workspace_id=s, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=s, user_id=seatless, role=WorkspaceRole.MEMBER),
                Context(
                    id=s_shared, workspace_id=s, name="s-shared", created_by=owner, is_private=False
                ),
                Context(
                    id=shared, workspace_id=x, name="shared", created_by=owner, is_private=False
                ),
                ExternalAPIKey(
                    key_name="openai-x",
                    provider="openai",
                    encrypted_value="unused",
                    user_id=owner,
                    workspace_id=x,
                    enabled=True,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                _memory(x, shared, owner, "Shared row that embedded", "success"),
                _memory(x, shared, owner, FAILED_SUMMARY, "failed"),
                _memory(s, s_shared, owner, FAILED_SUMMARY, "failed"),
            ]
        )
        await db.execute(
            update(User).where(User.user_id.in_([member, viewer])).values(current_workspace_id=x)
        )
        await db.execute(
            update(User).where(User.user_id == seatless).values(current_workspace_id=s)
        )
        await db.execute(
            update(User).where(User.user_id == outsider).values(current_workspace_id=y)
        )
        manager = APIKeyManager(db)
        # Inserted directly: a key row scoped to X whose user is not a member of X.
        outsider_key, _ = await manager.create_key(
            name="outside-x", user_id=outsider, workspace_id=x
        )
        member_key, _ = await manager.create_key(name="member-x", user_id=member, workspace_id=x)
        viewer_key, _ = await manager.create_key(name="viewer-x", user_id=viewer, workspace_id=x)
        seatless_key, _ = await manager.create_key(
            name="seatless-s", user_id=seatless, workspace_id=s
        )
        await db.commit()
    try:
        yield SimpleNamespace(
            factory=factory,
            app=_app(factory),
            owner=owner,
            member=member,
            outsider=outsider,
            x=x,
            y=y,
            outsider_key=outsider_key,
            member_key=member_key,
            viewer=viewer,
            viewer_key=viewer_key,
            seatless=seatless,
            seatless_key=seatless_key,
            s=s,
        )
    finally:
        async with factory() as db:
            await db.execute(delete(Memory).where(Memory.workspace_id.in_([x, y, s])))
            await db.execute(delete(APIKey).where(APIKey.user_id.in_(users)))
            await db.execute(delete(ExternalAPIKey).where(ExternalAPIKey.workspace_id.in_([x, y])))
            await db.execute(
                update(User).where(User.user_id.in_(users)).values(current_workspace_id=None)
            )
            await db.execute(delete(Context).where(Context.workspace_id.in_([x, y, s])))
            await db.execute(
                delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_([x, y, s]))
            )
            await db.execute(delete(Workspace).where(Workspace.id.in_([x, y, s])))
            await db.execute(delete(User).where(User.user_id.in_(users)))
            await db.commit()


async def _get(world, path: str, *, bearer: str | None = None, session: str | None = None):
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if session:
        headers["x-test-session"] = session
    async with AsyncClient(
        transport=ASGITransport(app=world.app), base_url="https://testserver"
    ) as client:
        return await client.get(path, headers=headers)


async def _select_x_for(world, user_id: str) -> None:
    """Store X as the user's current workspace, as a stale selection would."""
    async with world.factory() as db:
        await db.execute(
            update(User).where(User.user_id == user_id).values(current_workspace_id=world.x)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# GET /workspace/embedding-status
# ---------------------------------------------------------------------------


async def test_embedding_status_requires_membership_of_the_current_workspace(world):
    """A session whose stored current workspace is X gets the uniform refusal;
    a key scoped to X whose user is not a member of X does not authenticate."""
    path = "/api/v1/workspace/embedding-status"
    by_key = await _get(world, path, bearer=world.outsider_key)
    unknown = await _get(world, path, bearer="kagura_" + "x" * 43)
    await _select_x_for(world, world.outsider)
    by_session = await _get(world, path, session=world.outsider)

    assert (by_key.status_code, by_key.json()) == (401, unknown.json()), by_key.text
    assert (by_session.status_code, by_session.json()) == (403, REFUSAL)


async def test_embedding_status_for_a_member_is_unchanged(world):
    for kwargs in ({"bearer": world.member_key}, {"session": world.member}):
        response = await _get(world, "/api/v1/workspace/embedding-status", **kwargs)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        assert body["by_status"] == {"success": 1, "failed": 1}
        assert [m["summary"] for m in body["failed_memories"]] == [FAILED_SUMMARY]


# ---------------------------------------------------------------------------
# GET /workspace/stats and GET /system/embedding/models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/workspace/stats", "/api/v1/system/embedding/models"])
async def test_current_workspace_reads_require_membership(world, path):
    by_key = await _get(world, path, bearer=world.outsider_key)
    unknown = await _get(world, path, bearer="kagura_" + "x" * 43)
    await _select_x_for(world, world.outsider)
    by_session = await _get(world, path, session=world.outsider)

    assert (by_key.status_code, by_key.json()) == (401, unknown.json()), by_key.text
    assert (by_session.status_code, by_session.json()) == (403, REFUSAL)


async def test_workspace_stats_for_a_member_is_unchanged(world):
    for kwargs in ({"bearer": world.member_key}, {"session": world.member}):
        response = await _get(world, "/api/v1/workspace/stats", **kwargs)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total_memories"] == 2
        assert body["context_count"] == 1
        assert [c["context_name"] for c in body["contexts"]] == ["shared"]
        assert body["plan_name"] == "promax"


async def test_embedding_models_for_a_member_is_unchanged(world):
    for kwargs in ({"bearer": world.member_key}, {"session": world.member}):
        response = await _get(world, "/api/v1/system/embedding/models", **kwargs)
        assert response.status_code == 200, response.text
        openai = [m for m in response.json()["models"] if m["provider"] == "openai"]
        assert openai, response.text
        # The workspace's enabled OpenAI key makes the OpenAI models available.
        assert all(m["available"] is True for m in openai)


# ---------------------------------------------------------------------------
# MCP tools on the session workspace: get_usage, get_resource_impact,
# get_resource_schema
# ---------------------------------------------------------------------------


async def _mcp(
    world, tool: str, args: dict[str, Any], user_id: str, workspace_id: UUID | None = None
) -> dict[str, Any]:
    from mcp_server.tools import execute_tool_call
    from mcp_server.tools._helpers import _mcp_key_workspace_scope

    async def _db():
        async with world.factory() as db:
            yield db

    # The transport sets the session workspace to a scoped key's own workspace.
    workspace_id = workspace_id or world.x
    token = _mcp_key_workspace_scope.set(workspace_id)
    try:
        with (
            patch("db.base.get_db", new=_db),
            patch("mcp_server.tools.usage._log_tool_usage", new=AsyncMock()),
            patch("mcp_server.tools.resource._log_tool_usage", new=AsyncMock()),
        ):
            result = await execute_tool_call(tool, args, user_id, workspace_id)
    finally:
        _mcp_key_workspace_scope.reset(token)
    return json.loads(result[0].text)


async def _get_usage(world, user_id: str) -> dict[str, Any]:
    return await _mcp(world, "get_usage", {}, user_id)


@contextlib.asynccontextmanager
async def _resource_in(world, workspace_id: UUID) -> AsyncIterator[str]:
    """A resource of the workspace (its context, its ``resources`` row and a
    schema); removed on exit."""
    from models.resource import Resource, ResourceSchema
    from services.resource_lookup import upsert_resource

    slug = f"res-{uuid4().hex[:10]}"
    async with world.factory() as db:
        db.add(
            Context(
                id=uuid4(),
                workspace_id=workspace_id,
                name=slug,
                created_by=world.owner,
                is_private=False,
                is_public=True,
                resource_id=slug,
            )
        )
        await db.flush()
        pk = await upsert_resource(
            db, workspace_id=workspace_id, resource_id=slug, name=slug, created_by=world.owner
        )
        db.add(
            ResourceSchema(
                resource_pk=pk,
                resource_id=slug,
                schema_version=1,
                field_definitions=[{"name": "title", "type": "string"}],
            )
        )
        await db.commit()
    try:
        yield slug
    finally:
        async with world.factory() as db:
            await db.execute(delete(ResourceSchema).where(ResourceSchema.resource_pk == pk))
            await db.execute(delete(Resource).where(Resource.id == pk))
            await db.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def resource(world):
    """A resource of X."""
    async with _resource_in(world, world.x) as slug:
        yield slug


@pytest.mark.parametrize("tool", ["get_resource_impact", "get_resource_schema"])
async def test_mcp_resource_reads_require_membership_of_the_session_workspace(
    world, resource, tool
):
    body = await _mcp(world, tool, {"resource_id": resource}, world.outsider)

    assert body.get("error") == "permission_denied", body
    assert body.get("your_role") == "not_a_member"
    assert "field_definitions" not in body and "memory_count" not in body


async def test_mcp_resource_reads_for_a_member_are_unchanged(world, resource):
    impact = await _mcp(world, "get_resource_impact", {"resource_id": resource}, world.member)
    schema = await _mcp(world, "get_resource_schema", {"resource_id": resource}, world.member)

    assert impact.get("error") is None, impact
    assert (impact["memory_count"], impact["current_schema_version"]) == (0, 1)
    assert schema.get("error") is None, schema
    assert schema["field_definitions"] == [{"name": "title", "type": "string"}]


async def test_mcp_get_usage_requires_membership_of_the_session_workspace(world):
    body = await _get_usage(world, world.outsider)

    assert body.get("error") == "permission_denied", body
    assert body.get("your_role") == "not_a_member"
    assert "plan" not in body and "memories" not in body


async def test_mcp_get_usage_for_a_member_is_unchanged(world):
    body = await _get_usage(world, world.member)

    assert body.get("error") is None, body
    assert body["plan"] == "promax"
    assert body["memories"]["used"] == 2
    assert body["contexts"]["used"] == 1
    assert body["members"]["used"] == 3  # owner, member, viewer


# ---------------------------------------------------------------------------
# A viewer, and a member without a seat on the plan, keep their reads
# ---------------------------------------------------------------------------


async def test_a_viewer_keeps_the_stats_embedding_status_and_usage(world):
    for kwargs in ({"bearer": world.viewer_key}, {"session": world.viewer}):
        stats = await _get(world, "/api/v1/workspace/stats", **kwargs)
        assert stats.status_code == 200, stats.text
        # A viewer without a context access list reaches every shared context.
        assert [c["context_name"] for c in stats.json()["contexts"]] == ["shared"]
        status = await _get(world, "/api/v1/workspace/embedding-status", **kwargs)
        assert status.status_code == 200, status.text
        assert status.json()["by_status"] == {"success": 1, "failed": 1}

    usage = await _get_usage(world, world.viewer)
    assert usage.get("error") is None, usage
    assert usage["memories"]["used"] == 2


async def test_a_member_without_a_seat_keeps_the_stats_embedding_status_and_usage(world):
    for kwargs in ({"bearer": world.seatless_key}, {"session": world.seatless}):
        stats = await _get(world, "/api/v1/workspace/stats", **kwargs)
        assert stats.status_code == 200, stats.text
        body = stats.json()
        # Totals stay; without a seat no context is named.
        assert (body["total_memories"], body["contexts"], body["plan_name"]) == (1, [], "free")
        status = await _get(world, "/api/v1/workspace/embedding-status", **kwargs)
        assert status.status_code == 200, status.text
        assert status.json()["by_status"] == {"failed": 1}

    usage = await _mcp(world, "get_usage", {}, world.seatless, world.s)
    assert usage.get("error") is None, usage
    assert (usage["plan"], usage["memories"]["used"]) == ("free", 1)


@pytest.mark.parametrize("tool", ["get_resource_impact", "get_resource_schema"])
async def test_a_member_without_a_seat_keeps_the_resource_reads(world, tool):
    async with _resource_in(world, world.s) as slug:
        body = await _mcp(world, tool, {"resource_id": slug}, world.seatless, world.s)

    assert body.get("error") is None, body
    assert body["resource_id"] == slug
