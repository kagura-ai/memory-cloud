"""Shared rows and clients for the observability API tests (#2679).

``world`` builds two workspaces on the test database: A (owner + member, two
resources with their contexts) and B (another owner, one resource). Clients
run the real app with ``get_db`` on the test database; ``owner_client``
injects the caller for the owner routes, ``app_client`` sends whatever
headers the test gives (the observability routes read the bearer
themselves).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, User, Workspace, WorkspaceMember
from models.resource import Resource


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    tag: str
    owner: str
    member: str
    other_owner: str
    ws: UUID
    other_ws: UUID
    slugs: dict[str, str]
    resource_pk: dict[str, UUID]
    ctx: dict[str, UUID]
    extra_users: list[str] = field(default_factory=list)


async def _make_world(async_engine) -> World:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, member, other_owner = (f"obs-owner-{tag}", f"obs-member-{tag}", f"obs-other-{tag}")
    slugs = {"a1": f"cat-a1-{tag}", "a2": f"cat-a2-{tag}", "b1": f"cat-b1-{tag}"}
    resource_pk: dict[str, UUID] = {}
    ctx: dict[str, UUID] = {}
    async with factory() as s:
        for uid in (owner, member, other_owner):
            s.add(User(email=f"{uid}@example.test", user_id=uid, name=uid, role="user"))
        await s.flush()
        ws = Workspace(
            id=uuid4(),
            name=f"obs-a-{tag}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=50000,
            weekly_api_limit=250000,
        )
        other = Workspace(
            id=uuid4(),
            name=f"obs-b-{tag}",
            plan_name="pro",
            owner_user_id=other_owner,
            daily_api_limit=50000,
            weekly_api_limit=250000,
        )
        s.add_all([ws, other])
        await s.flush()
        s.add_all(
            [
                WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=ws.id, user_id=member, role=WorkspaceRole.MEMBER),
                WorkspaceMember(
                    workspace_id=other.id, user_id=other_owner, role=WorkspaceRole.OWNER
                ),
            ]
        )
        for key, slug in slugs.items():
            wsid = other.id if key.startswith("b") else ws.id
            creator = other_owner if key.startswith("b") else owner
            res = Resource(id=uuid4(), workspace_id=wsid, resource_id=slug, created_by=creator)
            s.add(res)
            await s.flush()
            context = Context(
                id=uuid4(),
                workspace_id=wsid,
                name=slug,
                created_by=creator,
                is_private=False,
                is_public=True,
                resource_id=slug,
            )
            s.add(context)
            resource_pk[key] = res.id
            ctx[key] = context.id
        await s.commit()
    return World(
        factory=factory,
        tag=tag,
        owner=owner,
        member=member,
        other_owner=other_owner,
        ws=ws.id,
        other_ws=other.id,
        slugs=slugs,
        resource_pk=resource_pk,
        ctx=ctx,
    )


async def _drop_world(w: World) -> None:
    async with w.factory() as s:
        await s.execute(delete(Workspace).where(Workspace.id.in_([w.ws, w.other_ws])))
        await s.execute(
            delete(User).where(User.user_id.in_([w.owner, w.member, w.other_owner, *w.extra_users]))
        )
        await s.commit()


@contextlib.asynccontextmanager
async def world_rows(async_engine) -> AsyncIterator[World]:
    """The two workspaces, removed again afterwards (wrap it in a module fixture)."""
    w = await _make_world(async_engine)
    try:
        yield w
    finally:
        await _drop_world(w)


def _db_override(w: World):
    async def _db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    return _db


@contextlib.asynccontextmanager
async def owner_client(
    w: World, user_id: str | None, workspace_id: UUID | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """A client whose caller is ``user_id`` in ``workspace_id`` (default A); ``None`` = anonymous."""
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db

    async def _user() -> dict[str, Any]:
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.test",
            "role": "user",
            "current_workspace_id": workspace_id or w.ws,
        }

    app.dependency_overrides[get_db] = _db_override(w)
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


@contextlib.asynccontextmanager
async def app_client(
    w: World, client_addr: tuple[str, int] = ("127.0.0.1", 1234)
) -> AsyncIterator[httpx.AsyncClient]:
    """A client with no injected caller: every credential goes through the real dependencies."""
    from api.main import app
    from db.base import get_db

    app.dependency_overrides[get_db] = _db_override(w)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=client_addr),
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


async def issue_credential(
    w: World,
    *,
    resource: str | None = None,
    workspace: str = "a",
    ttl_days: int | None = None,
) -> tuple[str, str]:
    """Issue a credential straight through the manager; returns ``(public_id, secret)``."""
    from auth.observation_credentials import ObservationCredentialManager

    ws = w.other_ws if workspace == "b" else w.ws
    by = w.other_owner if workspace == "b" else w.owner
    async with w.factory() as s:
        row, secret = await ObservationCredentialManager(s).create(
            ws,
            name=f"dash-{uuid4().hex[:6]}",
            resource_pk=w.resource_pk[resource] if resource else None,
            ttl_days=ttl_days,
            created_by=by,
        )
        await s.commit()
        return row.public_id, secret


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}
