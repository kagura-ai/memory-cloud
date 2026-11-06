"""Owner routes that publish a resource as a public MCP endpoint (#2675).

On real rows: the FastAPI app with ``get_db`` on the test database and the
caller injected through ``get_user_from_api_key_or_session`` (the real
``require_workspace_owner`` then checks the membership row).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.auth import Context, User, Workspace, WorkspaceMember
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from utils.public_id import PublicIdPrefix, public_id_pattern

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    owner: str
    member: str
    ws: UUID
    tag: str
    slugs: dict[str, str]
    ctx: dict[str, UUID]


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, member = f"pm-owner-{tag}", f"pm-member-{tag}"
    slugs = {
        "public": f"catalog-{tag}",
        "shared": f"shared-{tag}",
        "orphan": f"orphan-{tag}",
    }
    async with factory() as s:
        for uid in (owner, member):
            s.add(User(email=f"{uid}@example.test", user_id=uid, name=uid, role="user"))
        await s.flush()
        ws = Workspace(
            id=uuid4(),
            name=f"pm-{tag}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=50000,
            weekly_api_limit=250000,
        )
        s.add(ws)
        await s.flush()
        s.add_all(
            [
                WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=ws.id, user_id=member, role=WorkspaceRole.MEMBER),
            ]
        )
        for slug in slugs.values():
            s.add(Resource(workspace_id=ws.id, resource_id=slug, created_by=owner))
        public = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=slugs["public"],
            created_by=owner,
            is_private=False,
            is_public=True,
            resource_id=slugs["public"],
        )
        shared = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=slugs["shared"],
            created_by=owner,
            is_private=False,
            is_public=False,
            resource_id=slugs["shared"],
        )
        s.add_all([public, shared])
        await s.commit()
        world = World(
            factory=factory,
            owner=owner,
            member=member,
            ws=ws.id,
            tag=tag,
            slugs=slugs,
            ctx={"public": public.id, "shared": shared.id},
        )
    try:
        yield world
    finally:
        async with factory() as s:
            for stmt in (
                delete(Workspace).where(Workspace.id == world.ws),
                delete(User).where(User.user_id.in_([owner, member])),
            ):
                await s.execute(stmt)
            await s.commit()


@contextlib.asynccontextmanager
async def _client(w: World, user_id: str | None) -> AsyncIterator[httpx.AsyncClient]:
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db

    async def _db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def _user() -> dict[str, Any]:
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.test",
            "role": "user",
            "current_workspace_id": w.ws,
        }

    app.dependency_overrides[get_db] = _db
    if user_id is not None:
        app.dependency_overrides[get_user_from_api_key_or_session] = _user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_user_from_api_key_or_session, None)


def _path(slug: str) -> str:
    return f"/api/v1/resources/{slug}/public-mcp"


BODY = {
    "public_name": "Product catalog",
    "public_description": "Our public product data.",
    "public_usage_guide": "Search by product name.",
}


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)


async def test_anonymous_caller_is_refused(world: World) -> None:
    async with _client(world, None) as c:
        for method in ("POST", "GET", "PATCH", "DELETE"):
            r = await c.request(method, _path(world.slugs["public"]), json=BODY)
            assert r.status_code == 401, method


async def test_member_is_refused(world: World) -> None:
    async with _client(world, world.member) as c:
        for method in ("POST", "GET", "PATCH", "DELETE"):
            r = await c.request(method, _path(world.slugs["public"]), json=BODY)
            assert r.status_code == 403, method
    async with world.factory() as s:
        rows = (
            await s.execute(
                select(PublicMcpEndpoint).where(PublicMcpEndpoint.workspace_id == world.ws)
            )
        ).all()
        assert rows == []


async def test_owner_publishes_reads_edits_and_unpublishes(world: World, switch_on) -> None:
    async with _client(world, world.owner) as c:
        r = await c.post(_path(world.slugs["public"]), json=BODY)
        assert r.status_code == 201, r.text
        body = r.json()
        assert set(body) == {
            "public_id",
            "endpoint_path",
            "resource_id",
            "context_id",
            "public_name",
            "public_description",
            "public_usage_guide",
            "created_at",
            "updated_at",
            "serving",
        }
        pid = body["public_id"]
        assert re.fullmatch(public_id_pattern(PublicIdPrefix.PUBLIC_MCP), pid)
        assert body["endpoint_path"] == f"/mcp/public/{pid}"
        assert body["resource_id"] == world.slugs["public"]
        assert body["context_id"] == str(world.ctx["public"])
        assert body["public_name"] == "Product catalog"
        assert body["serving"] is True
        assert body["created_at"].endswith("Z")

        # A second live endpoint for the same resource is a conflict.
        r = await c.post(_path(world.slugs["public"]), json=BODY)
        assert r.status_code == 409

        r = await c.get(_path(world.slugs["public"]))
        assert r.status_code == 200
        assert r.json()["public_id"] == pid

        r = await c.patch(
            _path(world.slugs["public"]),
            json={"public_name": "Catalog v2", "public_usage_guide": ""},
        )
        assert r.status_code == 200
        edited = r.json()
        assert edited["public_id"] == pid
        assert edited["public_name"] == "Catalog v2"
        assert edited["public_usage_guide"] is None
        assert edited["public_description"] == "Our public product data."
        assert edited["updated_at"] is not None

        r = await c.patch(_path(world.slugs["public"]), json={"public_name": "  "})
        assert r.status_code == 422

        r = await c.delete(_path(world.slugs["public"]))
        assert r.status_code == 204
        r = await c.get(_path(world.slugs["public"]))
        assert r.status_code == 404
        r = await c.delete(_path(world.slugs["public"]))
        assert r.status_code == 404

        # Re-publishing mints a new address.
        r = await c.post(_path(world.slugs["public"]), json=BODY)
        assert r.status_code == 201
        assert r.json()["public_id"] != pid
        r = await c.delete(_path(world.slugs["public"]))
        assert r.status_code == 204

    async with world.factory() as s:
        rows = (
            (
                await s.execute(
                    select(PublicMcpEndpoint).where(PublicMcpEndpoint.workspace_id == world.ws)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(row.revoked_at is not None for row in rows)
        assert all(row.revoked_by == world.owner for row in rows)
        assert all(row.created_by == world.owner for row in rows)


async def test_preconditions_are_named(world: World, switch_on) -> None:
    async with _client(world, world.owner) as c:
        r = await c.post(_path(world.slugs["shared"]), json=BODY)
        assert r.status_code == 422
        assert r.json()["details"]["precondition"] == "context_public"

        r = await c.post(_path(world.slugs["orphan"]), json=BODY)
        assert r.status_code == 422
        assert r.json()["details"]["precondition"] == "context_bound"

        r = await c.post(_path(f"missing-{world.tag}"), json=BODY)
        assert r.status_code == 404

        r = await c.post(_path("Not_A_Slug!"), json=BODY)
        assert r.status_code == 422

        r = await c.post(_path(world.slugs["public"]), json={"public_name": ""})
        assert r.status_code == 422
        r = await c.post(
            _path(world.slugs["public"]),
            json={"public_name": "x", "public_usage_guide": "y" * 4001},
        )
        assert r.status_code == 422


async def test_switch_off_still_manages_but_does_not_serve(world: World) -> None:
    assert get_settings().mcp_public_endpoints_enabled is False
    async with _client(world, world.owner) as c:
        r = await c.post(_path(world.slugs["public"]), json=BODY)
        assert r.status_code == 201
        assert r.json()["serving"] is False
        r = await c.get(_path(world.slugs["public"]))
        assert r.json()["serving"] is False
        r = await c.delete(_path(world.slugs["public"]))
        assert r.status_code == 204


async def test_serving_turns_false_when_the_context_stops_being_public(
    world: World, switch_on
) -> None:
    async with _client(world, world.owner) as c:
        r = await c.post(_path(world.slugs["public"]), json=BODY)
        assert r.status_code == 201
        assert r.json()["serving"] is True
        async with world.factory() as s:
            ctx = await s.get(Context, world.ctx["public"])
            assert ctx is not None
            ctx.is_public = False
            await s.commit()
        try:
            r = await c.get(_path(world.slugs["public"]))
            assert r.json()["serving"] is False
        finally:
            async with world.factory() as s:
                ctx = await s.get(Context, world.ctx["public"])
                assert ctx is not None
                ctx.is_public = True
                await s.commit()
            r = await c.delete(_path(world.slugs["public"]))
            assert r.status_code == 204
