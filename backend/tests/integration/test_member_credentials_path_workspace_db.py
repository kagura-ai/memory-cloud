"""The member-credential routes require membership of the workspace in the path (#2928).

On real PostgreSQL rows, over HTTP: the session dependency and API-key
authentication run as in the app, and every request gets its own session.

* every self-service route (the caller acts on their own credentials) answers a
  signed-in user who is not a member of the path workspace with the uniform
  ``AUTH-101`` refusal — the same body for an unknown workspace id — and
  changes nothing: no key is minted, hidden, rotated or deleted, no OAuth app
  changes, no operation-history record is written;
* a member keeps minting, listing, hiding, rotating and deleting their own
  key, and a member without a seat on the plan still hides and deletes theirs;
* the owner-provisioned mint and the routes acting on another member keep
  requiring the owner or a manager role of the path workspace.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from auth.api_keys import APIKeyManager
from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, OAuth2Client, User, Workspace, WorkspaceMember
from models.operation_history import OperationHistory
from services.plan_seats import member_seat_suspended

pytestmark = pytest.mark.asyncio(loop_scope="session")

REFUSAL = {"error": "AUTH-101", "message": "Insufficient permissions", "details": {}}


def _app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    from api.main import http_exception_handler, memory_cloud_exception_handler
    from api.routes import member_credentials, workspace
    from db.base import get_db
    from utils.exceptions import MemoryCloudException

    app = FastAPI()
    app.include_router(member_credentials.router)
    app.include_router(workspace.router, prefix="/api/v1")
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
    """Workspace X (owner + member), the outsider's own workspace Y, and a
    one-seat workspace S where the member has no seat. Keys and OAuth apps of
    the outsider exist so a route that acted would change something."""
    from api.routes import member_credentials

    monkeypatch.setattr(member_credentials, "schedule_security_notification", MagicMock())
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, member, outsider = (f"mc-{role}-{tag}" for role in ("owner", "member", "outsider"))
    x, y, s = uuid4(), uuid4(), uuid4()
    async with factory() as db:
        db.add_all(
            [
                User(user_id=uid, email=f"{uid}@example.test", name=uid, role="user")
                for uid in (owner, member, outsider)
            ]
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
                WorkspaceMember(workspace_id=x, user_id=member, role=WorkspaceRole.MEMBER),
                WorkspaceMember(workspace_id=y, user_id=outsider, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=s, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=s, user_id=member, role=WorkspaceRole.MEMBER),
            ]
        )
        await db.flush()
        await db.execute(
            update(User).where(User.user_id == outsider).values(current_workspace_id=y)
        )
        manager = APIKeyManager(db)
        # Rows inserted directly: a key of the outsider scoped to X, and their
        # account-wide key.
        _, outsider_x_key = await manager.create_key(
            name="outsider-x", user_id=outsider, workspace_id=x
        )
        outsider_global_plain, outsider_global_key = await manager.create_key(
            name="outsider-global", user_id=outsider
        )
        outsider_y_plain, _ = await manager.create_key(
            name="outsider-y", user_id=outsider, workspace_id=y
        )
        _, member_x_key = await manager.create_key(name="member-x", user_id=member, workspace_id=x)
        _, member_s_key = await manager.create_key(name="member-s", user_id=member, workspace_id=s)
        for owner_id, client_id in ((outsider, f"oc-out-{tag}"), (member, f"oc-mem-{tag}")):
            db.add(
                OAuth2Client(
                    client_id=client_id,
                    client_secret_hash="0" * 64,
                    client_name=client_id,
                    owner_id=owner_id,
                    workspace_id=x,
                    redirect_uris=["https://example.com/callback"],
                )
            )
        await db.commit()
        world = SimpleNamespace(
            factory=factory,
            app=_app(factory),
            owner=owner,
            member=member,
            outsider=outsider,
            x=x,
            y=y,
            s=s,
            outsider_x_key=outsider_x_key.public_id,
            outsider_global_key=outsider_global_key.public_id,
            outsider_global_plain=outsider_global_plain,
            outsider_y_plain=outsider_y_plain,
            member_x_key=member_x_key.public_id,
            member_s_key=member_s_key.public_id,
        )
    users = [owner, member, outsider]
    try:
        yield world
    finally:
        async with factory() as db:
            await db.execute(
                delete(OperationHistory).where(OperationHistory.workspace_id.in_([x, y, s]))
            )
            await db.execute(delete(APIKey).where(APIKey.user_id.in_(users)))
            await db.execute(delete(OAuth2Client).where(OAuth2Client.owner_id.in_(users)))
            await db.execute(
                update(User).where(User.user_id.in_(users)).values(current_workspace_id=None)
            )
            await db.execute(
                delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_([x, y, s]))
            )
            await db.execute(delete(Workspace).where(Workspace.id.in_([x, y, s])))
            await db.execute(delete(User).where(User.user_id.in_(users)))
            await db.commit()


async def _call(world, method: str, path: str, *, session=None, bearer=None, body=None):
    headers = {}
    if session:
        headers["x-test-session"] = session
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    async with AsyncClient(
        transport=ASGITransport(app=world.app), base_url="https://testserver"
    ) as client:
        return await client.request(method, path, headers=headers, json=body)


def _member_path(workspace_id: UUID, user_id: str, suffix: str) -> str:
    return f"/api/v1/workspaces/{workspace_id}/members/{user_id}/credentials{suffix}"


async def _state(world) -> tuple:
    """Everything a credential route could change, read fresh."""
    async with world.factory() as db:
        keys = (
            await db.execute(
                select(
                    APIKey.public_id,
                    APIKey.user_id,
                    APIKey.workspace_id,
                    APIKey.key_hash,
                    APIKey.hidden_at,
                    APIKey.revoked_at,
                )
                .where(APIKey.user_id.in_([world.owner, world.member, world.outsider]))
                .order_by(APIKey.public_id)
            )
        ).all()
        apps = (
            await db.execute(
                select(
                    OAuth2Client.client_id,
                    OAuth2Client.client_secret_hash,
                    OAuth2Client.hidden_at,
                )
                .where(OAuth2Client.owner_id.in_([world.member, world.outsider]))
                .order_by(OAuth2Client.client_id)
            )
        ).all()
        return tuple(keys), tuple(apps)


async def _history_count(world, *workspace_ids: UUID) -> int:
    async with world.factory() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(OperationHistory)
                .where(OperationHistory.workspace_id.in_(workspace_ids))
            )
            or 0
        )


# (method, path suffix, body) of every self-service route; "{key}" is the
# caller's own account-wide key.
SELF_SERVICE_ROUTES = [
    ("GET", "", None),
    ("POST", "/api-keys", {"name": "minted-here"}),
    ("POST", "/api-key/hide", None),
    ("POST", "/api-key/regenerate", None),
    ("DELETE", "/api-key", None),
    ("DELETE", "/api-keys/{key}", None),
    ("POST", "/oauth/hide", None),
    ("POST", "/oauth/regenerate", None),
    ("DELETE", "/oauth", None),
]


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    SELF_SERVICE_ROUTES,
    ids=[f"{m} {p or '/'}" for m, p, _ in SELF_SERVICE_ROUTES],
)
async def test_self_service_routes_require_membership_of_the_path_workspace(
    world, method, suffix, body
):
    """A signed-in user who is not a member of X gets the uniform refusal on
    every self-service route of X, the same body as for an unknown workspace
    id, and nothing changes."""
    suffix = suffix.replace("{key}", world.outsider_global_key)
    before = await _state(world)
    unknown = uuid4()

    on_x = await _call(
        world,
        method,
        _member_path(world.x, world.outsider, suffix),
        session=world.outsider,
        body=body,
    )
    on_unknown = await _call(
        world,
        method,
        _member_path(unknown, world.outsider, suffix),
        session=world.outsider,
        body=body,
    )

    assert (on_x.status_code, on_x.json()) == (403, REFUSAL), (
        f"{method} {suffix or '/'} on a workspace the caller is not a member of"
    )
    assert (on_unknown.status_code, on_unknown.json()) == (403, REFUSAL)
    assert await _state(world) == before, "a key or an OAuth app changed"
    assert await _history_count(world, world.x, unknown) == 0


async def test_a_member_keeps_managing_their_own_key(world):
    """Mint, list, hide, rotate and delete stay as they are for a member."""
    path = lambda suffix: _member_path(world.x, world.member, suffix)  # noqa: E731

    minted = await _call(
        world, "POST", path("/api-keys"), session=world.member, body={"name": "member-new"}
    )
    assert minted.status_code == 201, minted.text
    assert minted.json()["plaintext_key"]

    listed = await _call(world, "GET", path(""), session=world.member)
    assert listed.status_code == 200, listed.text
    assert {k["id"] for k in listed.json()["api_keys"]} == {
        world.member_x_key,
        minted.json()["id"],
    }
    assert listed.json()["target_user_role"] == "member"

    hidden = await _call(world, "POST", path("/api-key/hide"), session=world.member)
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == {"status": "hidden", "key_id": minted.json()["id"]}

    rotated = await _call(world, "POST", path("/api-key/regenerate"), session=world.member)
    assert rotated.status_code == 200, rotated.text

    deleted = await _call(world, "DELETE", path("/api-key"), session=world.member)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted", "key_id": rotated.json()["key_id"]}

    by_id = await _call(
        world, "DELETE", path(f"/api-keys/{world.member_x_key}"), session=world.member
    )
    assert by_id.status_code == 200, by_id.text
    assert by_id.json() == {"status": "deleted", "key_id": world.member_x_key}

    oauth = await _call(world, "POST", path("/oauth/hide"), session=world.member)
    assert oauth.status_code == 200, oauth.text
    assert await _history_count(world, world.x) > 0


async def test_a_member_without_a_seat_still_hides_and_deletes_their_own_key(world):
    async with world.factory() as db:
        workspace = await db.get(Workspace, world.s)
        row = await db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == world.s, WorkspaceMember.user_id == world.member
            )
        )
        assert workspace is not None and row is not None
        assert await member_seat_suspended(db, workspace, row)
    path = lambda suffix: _member_path(world.s, world.member, suffix)  # noqa: E731

    hidden = await _call(world, "POST", path("/api-key/hide"), session=world.member)
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == {"status": "hidden", "key_id": world.member_s_key}

    deleted = await _call(
        world, "DELETE", path(f"/api-keys/{world.member_s_key}"), session=world.member
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted", "key_id": world.member_s_key}


@pytest.mark.parametrize("key", ["global", "scoped_elsewhere"])
async def test_owner_provisioned_mint_requires_the_owner_of_the_path_workspace(world, key):
    """An owner key of another workspace (or an account-wide key of a
    non-member) cannot mint a key for a member of X."""
    before = await _state(world)
    bearer = world.outsider_global_plain if key == "global" else world.outsider_y_plain

    response = await _call(
        world,
        "POST",
        _member_path(world.x, world.member, "/api-keys"),
        bearer=bearer,
        body={"name": "provisioned", "expires_days": 30},
    )

    assert response.status_code in (403, 404), response.text
    assert response.json()["error"] in ("AUTH-101", "RES-001")
    assert await _state(world) == before
    assert await _history_count(world, world.x) == 0


ACTING_ON_ANOTHER_MEMBER = [
    ("GET", ""),
    ("POST", "/api-key/regenerate"),
    ("DELETE", "/api-key"),
    ("POST", "/oauth/regenerate"),
    ("DELETE", "/oauth"),
]


@pytest.mark.parametrize(
    ("method", "suffix"),
    ACTING_ON_ANOTHER_MEMBER,
    ids=[f"{m} {p or '/'}" for m, p in ACTING_ON_ANOTHER_MEMBER],
)
async def test_routes_acting_on_another_member_require_a_manager_role(world, method, suffix):
    """A signed-in non-member cannot view, rotate or delete a member's
    credentials of X; nothing changes."""
    before = await _state(world)

    response = await _call(
        world, method, _member_path(world.x, world.member, suffix), session=world.outsider
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "AUTH-101"
    assert await _state(world) == before
    assert await _history_count(world, world.x) == 0
