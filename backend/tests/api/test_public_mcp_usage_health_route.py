"""Owner health route of public MCP usage events (#2678), on real rows.

``get_db`` is the test database and the caller is injected through
``get_user_from_api_key_or_session``; the real ``require_workspace_owner``
and ``resolve_resource_by_slug`` run.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.public_mcp_usage_writer as writer_module
from auth.workspace_roles import WorkspaceRole
from models.auth import Context, User, Workspace, WorkspaceMember
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_usage import PublicMcpUsageEvent
from models.resource import Resource
from services.public_mcp_usage_events import UsageEventInput, build_gap_row, build_usage_event_row
from services.public_mcp_usage_writer import PublicMcpUsageWriter
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    owner: str
    member: str
    ws: UUID
    other_ws: UUID
    slug: str
    other_slug: str
    ctx: UUID
    resource_pk: UUID


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, member, stranger = f"uh-owner-{tag}", f"uh-member-{tag}", f"uh-other-{tag}"
    slug, other_slug = f"catalog-{tag}", f"foreign-{tag}"
    async with factory() as s:
        await s.execute(delete(PublicMcpUsageEvent))
        for uid in (owner, member, stranger):
            s.add(User(email=f"{uid}@example.test", user_id=uid, name=uid, role="user"))
        await s.flush()
        ws = Workspace(id=uuid4(), name=f"uh-{tag}", plan_name="pro", owner_user_id=owner)
        other = Workspace(id=uuid4(), name=f"uh2-{tag}", plan_name="pro", owner_user_id=stranger)
        s.add_all([ws, other])
        await s.flush()
        s.add_all(
            [
                WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=ws.id, user_id=member, role=WorkspaceRole.MEMBER),
                WorkspaceMember(workspace_id=other.id, user_id=stranger, role=WorkspaceRole.OWNER),
            ]
        )
        resource = Resource(workspace_id=ws.id, resource_id=slug, created_by=owner)
        s.add(resource)
        s.add(Resource(workspace_id=other.id, resource_id=other_slug, created_by=stranger))
        ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=slug,
            created_by=owner,
            is_private=False,
            is_public=True,
            resource_id=slug,
        )
        s.add(ctx)
        s.add(
            Context(
                id=uuid4(),
                workspace_id=other.id,
                name=other_slug,
                created_by=stranger,
                is_private=False,
                is_public=True,
                resource_id=other_slug,
            )
        )
        await s.commit()
        world = World(
            factory=factory,
            owner=owner,
            member=member,
            ws=ws.id,
            other_ws=other.id,
            slug=slug,
            other_slug=other_slug,
            ctx=ctx.id,
            resource_pk=resource.id,
        )
    try:
        yield world
    finally:
        async with factory() as s:
            await s.execute(delete(PublicMcpUsageEvent))
            for stmt in (
                delete(Workspace).where(Workspace.id.in_([world.ws, world.other_ws])),
                delete(User).where(User.user_id.in_([owner, member, stranger])),
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
    return f"/api/v1/resources/{slug}/public-mcp/usage-health"


async def _endpoint(w: World, *, revoked: bool) -> UUID:
    async with w.factory() as s:
        row = (
            await s.execute(
                insert(PublicMcpEndpoint)
                .values(
                    workspace_id=w.ws,
                    context_id=w.ctx,
                    resource_pk=w.resource_pk,
                    resource_id=w.slug,
                    public_name="Catalog",
                    created_by=w.owner,
                    revoked_at=utcnow() if revoked else None,
                )
                .returning(PublicMcpEndpoint.id)
            )
        ).scalar_one()
        await s.commit()
    return row


def _event(w: World, endpoint_id: UUID, outcome: str, age_hours: float = 1) -> dict:
    return build_usage_event_row(
        UsageEventInput(
            endpoint_id=endpoint_id,
            workspace_id=w.ws,
            context_id=w.ctx,
            resource_pk=w.resource_pk,
            resource_id=w.slug,
            tool="recall",
            outcome=outcome,
            era="stateless",
            started_at=utcnow() - timedelta(hours=age_hours),
            duration_ms=4,
        ),
        key="k" * 40,
        session_details_enabled=False,
    ).row


async def _insert(w: World, rows: list[dict]) -> None:
    async with w.factory() as s:
        await s.execute(insert(PublicMcpUsageEvent).values(rows))
        await s.commit()


async def test_anonymous_caller_is_refused(world: World) -> None:
    async with _client(world, None) as c:
        assert (await c.get(_path(world.slug))).status_code == 401


async def test_member_is_refused(world: World) -> None:
    async with _client(world, world.member) as c:
        assert (await c.get(_path(world.slug))).status_code == 403


async def test_foreign_slug_is_not_found(world: World) -> None:
    async with _client(world, world.owner) as c:
        assert (await c.get(_path(world.other_slug))).status_code == 404


async def test_unpublished_resource_has_no_endpoint(world: World) -> None:
    async with _client(world, world.owner) as c:
        r = await c.get(_path(world.slug))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint_id"] is None
    assert body["events"] == 0
    assert body["outcomes"] == {}


async def test_owner_sees_live_endpoint_counts_gaps_and_writer(world: World, monkeypatch) -> None:
    old = await _endpoint(world, revoked=True)
    live = await _endpoint(world, revoked=False)
    start = utcnow() - timedelta(minutes=5)
    stale_gap = build_gap_row(
        dropped_count=50, started_at=start, ended_at=start, reason="queue_full"
    )
    stale_gap["occurred_at"] = utcnow() - timedelta(hours=30)
    await _insert(
        world,
        [
            # The old (unpublished) endpoint's events stay out of the live view.
            _event(world, old, "success"),
            _event(world, old, "success"),
            _event(world, live, "success"),
            _event(world, live, "success"),
            _event(world, live, "denied"),
            _event(world, live, "cancelled"),
            _event(world, live, "success", age_hours=30),  # outside the window
            build_gap_row(dropped_count=3, started_at=start, ended_at=start, reason="write_failed"),
            stale_gap,
        ],
    )
    writer = PublicMcpUsageWriter(
        session_factory=world.factory,
        queue_size=150,
        flush_interval=1.0,
        write_timeout=1.0,
        key="k" * 40,
        session_details_enabled=False,
    )
    monkeypatch.setattr(writer_module, "_writer", writer)

    async with _client(world, world.owner) as c:
        r = await c.get(_path(world.slug))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "resource_id",
        "endpoint_id",
        "window_hours",
        "events",
        "outcomes",
        "gaps",
        "writer",
        "retention_days",
    }
    assert body["resource_id"] == world.slug
    assert body["endpoint_id"] == str(live)
    assert body["window_hours"] == 24
    assert body["events"] == 4
    assert body["outcomes"] == {"success": 2, "denied": 1, "cancelled": 1}
    assert body["gaps"]["count"] == 1
    assert body["gaps"]["dropped_events"] == 3
    assert body["gaps"]["last_gap_at"].endswith("Z")
    assert body["writer"] == {
        "enabled": True,
        "queue_depth": 0,
        "queue_capacity": 150,
        "dropped_since_start": 0,
        "last_flush_at": None,
        "last_flush_ok": None,
        "process_local": True,
    }
    assert body["retention_days"] == 90


async def test_writer_off_is_reported(world: World, monkeypatch) -> None:
    monkeypatch.setattr(writer_module, "_writer", None)
    async with _client(world, world.owner) as c:
        r = await c.get(_path(world.slug))
    assert r.status_code == 200
    assert r.json()["writer"]["enabled"] is False
    assert r.json()["writer"]["process_local"] is True
