"""The config API key routes and the membership of a key's workspace (#2928).

On real PostgreSQL rows, over HTTP with the session dependency:

* ``POST /config/api-keys/{key_id}/regenerate`` rotates a workspace-scoped key
  only while its owner holds the key's workspace; otherwise it answers the
  route's 404 and changes nothing;
* deleting or revoking one's own key works for everyone; the operation-history
  record goes to the key's workspace only when the owner holds it (a key whose
  owner does not is recorded like a global key: not at all);
* a member's key: regenerate, revoke and delete record into the workspace as
  before;
* a public-bound key is held through the workspace of its bound context: the
  same rules for a bound key whose owner has no membership row there, and for
  a member's bound key.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from auth.api_keys import APIKeyManager
from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, Context, User, Workspace, WorkspaceMember
from models.operation_history import OperationHistory

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOT_FOUND = {
    "error": "HTTP-404",
    "message": "API key not found or not owned by you",
    "details": {},
}


def _app(factory) -> FastAPI:
    from api.main import http_exception_handler, memory_cloud_exception_handler
    from api.routes import api_keys
    from db.base import get_db
    from utils.exceptions import MemoryCloudException

    app = FastAPI()
    app.include_router(api_keys.router, prefix="/api/v1")
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
            await db.commit()

    app.dependency_overrides[get_db] = request_db
    return app


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine, monkeypatch, fernet_api_key_secret):
    """Workspace X (owner + member); an outsider holding a key row scoped to X."""
    from api.routes import api_keys

    monkeypatch.setattr(api_keys, "schedule_security_notification", MagicMock())
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, member, outsider = (f"ck-{role}-{tag}" for role in ("owner", "member", "outsider"))
    users = [owner, member, outsider]
    x = uuid4()
    async with factory() as db:
        db.add_all(
            [User(user_id=uid, email=f"{uid}@example.test", name=uid, role="user") for uid in users]
        )
        await db.flush()
        db.add(Workspace(id=x, name=f"ck-{tag}", owner_user_id=owner, plan_name="promax"))
        await db.flush()
        db.add_all(
            [
                WorkspaceMember(workspace_id=x, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=x, user_id=member, role=WorkspaceRole.MEMBER),
            ]
        )
        await db.flush()
        manager = APIKeyManager(db)
        # Inserted directly: a key row scoped to X whose owner is not in X.
        _, outsider_key = await manager.create_key(
            name="outside-x", user_id=outsider, workspace_id=x
        )
        _, member_key = await manager.create_key(name="member-x", user_id=member, workspace_id=x)
        await db.commit()
        world = SimpleNamespace(
            factory=factory,
            app=_app(factory),
            member=member,
            outsider=outsider,
            x=x,
            outsider_key=outsider_key.public_id,
            member_key=member_key.public_id,
        )
    try:
        yield world
    finally:
        async with factory() as db:
            await db.execute(delete(OperationHistory).where(OperationHistory.workspace_id == x))
            await db.execute(delete(APIKey).where(APIKey.user_id.in_(users)))
            await db.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == x))
            await db.execute(delete(Workspace).where(Workspace.id == x))
            await db.execute(delete(User).where(User.user_id.in_(users)))
            await db.commit()


async def _call(world, method: str, path: str, user_id: str):
    async with AsyncClient(
        transport=ASGITransport(app=world.app), base_url="https://testserver"
    ) as client:
        return await client.request(
            method, f"/api/v1/config/api-keys{path}", headers={"x-test-session": user_id}
        )


async def _history(world) -> list[tuple[str, str | None]]:
    async with world.factory() as db:
        rows = await db.execute(
            select(OperationHistory.operation, OperationHistory.target_id)
            .where(OperationHistory.workspace_id == world.x)
            .order_by(OperationHistory.operation)
        )
        return [(op, target) for op, target in rows.all()]


async def _keys_of(world, user_id: str) -> list[tuple[str, bool]]:
    async with world.factory() as db:
        rows = await db.execute(
            select(APIKey.public_id, APIKey.revoked_at.is_not(None))
            .where(APIKey.user_id == user_id)
            .order_by(APIKey.public_id)
        )
        return [(public_id, bool(revoked)) for public_id, revoked in rows.all()]


async def test_regenerate_requires_the_owner_to_hold_the_key_workspace(world):
    before = await _keys_of(world, world.outsider)

    response = await _call(world, "POST", f"/{world.outsider_key}/regenerate", world.outsider)

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert await _keys_of(world, world.outsider) == before
    assert await _history(world) == []


@pytest.mark.parametrize(("method", "suffix"), [("DELETE", ""), ("POST", "/revoke")])
async def test_deleting_or_revoking_ones_own_key_records_only_into_a_held_workspace(
    world, method, suffix
):
    response = await _call(world, method, f"/{world.outsider_key}{suffix}", world.outsider)

    assert response.status_code == 204, response.text
    remaining = dict(await _keys_of(world, world.outsider))
    assert remaining.get(world.outsider_key, True) is True  # deleted or revoked
    assert await _history(world) == []


async def test_clearing_the_default_context_of_ones_own_key_still_works(world):
    async with AsyncClient(
        transport=ASGITransport(app=world.app), base_url="https://testserver"
    ) as client:
        response = await client.patch(
            f"/api/v1/config/api-keys/{world.outsider_key}",
            headers={"x-test-session": world.outsider},
            json={"default_context_id": None},
        )

    assert response.status_code == 200, response.text
    assert await _history(world) == []


async def test_a_members_key_regenerates_and_records_into_the_workspace(world):
    response = await _call(world, "POST", f"/{world.member_key}/regenerate", world.member)

    assert response.status_code == 200, response.text
    new_key = response.json()["id"]
    assert await _history(world) == [
        ("credential.issued", new_key),
        ("credential.revoked", world.member_key),
    ]


@pytest.mark.parametrize(("method", "suffix"), [("DELETE", ""), ("POST", "/revoke")])
async def test_a_members_delete_or_revoke_records_into_the_workspace(world, method, suffix):
    response = await _call(world, method, f"/{world.member_key}{suffix}", world.member)

    assert response.status_code == 204, response.text
    assert await _history(world) == [("credential.revoked", world.member_key)]


async def test_listing_ones_own_keys_shows_every_key(world):
    response = await _call(world, "GET", "", world.outsider)

    assert response.status_code == 200, response.text
    assert [k["id"] for k in response.json()] == [world.outsider_key]
    async with world.factory() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(APIKey).where(APIKey.user_id == world.outsider)
            )
            == 1
        )


# ---------------------------------------------------------------------------
# Public-bound keys: held through the workspace of the bound context
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def bound(world):
    """A public context of X with two public-bound keys: the member's, and
    one of the outsider, who holds no membership row in X."""
    context_id = uuid4()
    async with world.factory() as db:
        db.add(
            Context(
                id=context_id,
                workspace_id=world.x,
                name=f"ck-pub-{uuid4().hex[:8]}",
                created_by=world.member,
                is_public=True,
            )
        )
        await db.flush()
        manager = APIKeyManager(db)
        _, member_bound = await manager.create_key(
            name="member-bound", user_id=world.member, bound_context_id=context_id
        )
        _, outsider_bound = await manager.create_key(
            name="outside-bound", user_id=world.outsider, bound_context_id=context_id
        )
        await db.commit()
        bound = SimpleNamespace(
            member_key=member_bound.public_id, outsider_key=outsider_bound.public_id
        )
    try:
        yield bound
    finally:
        async with world.factory() as db:
            await db.execute(delete(APIKey).where(APIKey.bound_context_id == context_id))
            await db.execute(delete(Context).where(Context.id == context_id))
            await db.commit()


async def test_a_bound_key_is_rotated_only_while_its_owner_holds_the_contexts_workspace(
    world, bound
):
    before = await _keys_of(world, world.outsider)

    response = await _call(world, "POST", f"/{bound.outsider_key}/regenerate", world.outsider)

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert await _keys_of(world, world.outsider) == before
    assert await _history(world) == []


@pytest.mark.parametrize(("method", "suffix"), [("DELETE", ""), ("POST", "/revoke")])
async def test_deleting_or_revoking_a_bound_key_records_only_into_a_held_workspace(
    world, bound, method, suffix
):
    response = await _call(world, method, f"/{bound.outsider_key}{suffix}", world.outsider)

    assert response.status_code == 204, response.text
    remaining = dict(await _keys_of(world, world.outsider))
    assert remaining.get(bound.outsider_key, True) is True  # deleted or revoked
    assert await _history(world) == []


async def test_a_members_bound_key_regenerates_and_records_into_the_workspace(world, bound):
    response = await _call(world, "POST", f"/{bound.member_key}/regenerate", world.member)

    assert response.status_code == 200, response.text
    new_key = response.json()["id"]
    assert await _history(world) == [
        ("credential.issued", new_key),
        ("credential.revoked", bound.member_key),
    ]


@pytest.mark.parametrize(("method", "suffix"), [("DELETE", ""), ("POST", "/revoke")])
async def test_a_members_bound_key_delete_or_revoke_records_into_the_workspace(
    world, bound, method, suffix
):
    response = await _call(world, method, f"/{bound.member_key}{suffix}", world.member)

    assert response.status_code == 204, response.text
    assert await _history(world) == [("credential.revoked", bound.member_key)]
