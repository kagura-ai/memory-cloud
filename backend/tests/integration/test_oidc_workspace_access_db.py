"""Real PostgreSQL HTTP checks for live identity membership and access (#2793)."""

from __future__ import annotations

import secrets
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes import me_workspaces
from auth import oidc_limits
from auth.oidc_server import credential_hash
from config.settings import get_settings
from db.base import get_db
from models.auth import Context, ContextMember, User, Workspace, WorkspaceMember
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from tests.api.test_me_workspaces import assert_private
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def store(async_engine, monkeypatch):
    """Persist private fixture rows and give each HTTP request a fresh async session."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    prefix = "workspace-oidc-" + uuid4().hex
    users = {
        role: prefix + "-" + role for role in ("owner", "admin", "member", "viewer", "outsider")
    }
    wid, other_wid = uuid4(), uuid4()
    shared, private, foreign = uuid4(), uuid4(), uuid4()
    tokens, token_ids, grant_ids, member_ids = {}, {}, {}, {}
    async with factory() as db:
        db.add_all(
            [
                User(
                    user_id=uid,
                    email=uid + "@example.test",
                    role="admin" if role == "outsider" else "user",
                )
                for role, uid in users.items()
            ]
        )
        await db.flush()
        db.add_all(
            [
                Workspace(
                    id=wid,
                    name="Primary",
                    owner_user_id=users["owner"],
                    plan_name="promax",
                    addon_member_bonus=20,
                ),
                Workspace(
                    id=other_wid, name="Other", owner_user_id=users["owner"], plan_name="promax"
                ),
                OIDCClient(
                    client_id=prefix,
                    client_name="Workspace access fixture",
                    client_secret_hash="unused-verifier",
                    redirect_uris=["https://client.example.test/callback"],
                    scope="openid workspaces:read",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Context(
                    id=shared,
                    workspace_id=wid,
                    name="shared",
                    created_by=users["owner"],
                    is_private=False,
                    created_at=utcnow() - timedelta(days=2),
                ),
                Context(
                    id=private,
                    workspace_id=wid,
                    name="private",
                    created_by=users["member"],
                    is_private=True,
                ),
                Context(
                    id=foreign,
                    workspace_id=other_wid,
                    name="foreign",
                    created_by=users["owner"],
                    is_private=False,
                ),
            ]
        )
        await db.flush()
        for role, uid in users.items():
            if role != "outsider":
                member = WorkspaceMember(
                    workspace_id=wid,
                    user_id=uid,
                    role=role,
                    allowed_context_ids=[shared, private] if role == "member" else None,
                )
                db.add(member)
                await db.flush()
                member_ids[role] = member.id
            grant = OIDCGrant(
                client_id=prefix,
                user_id=uid,
                scope="openid workspaces:read",
                nonce=uuid4().hex,
                auth_time=int(time.time()),
                expires_at=utcnow() + timedelta(hours=8),
            )
            db.add(grant)
            await db.flush()
            token = "kmc_oidc_at_" + secrets.token_urlsafe(32)
            pair = OIDCToken(
                grant_id=grant.id,
                access_token_hash=credential_hash(token),
                refresh_token_hash=credential_hash(secrets.token_urlsafe(32)),
                scope="openid workspaces:read",
                issued_at=int(time.time()),
                expires_in=300,
            )
            db.add(pair)
            await db.flush()
            tokens[role], token_ids[role], grant_ids[role] = token, pair.id, grant.id
        db.add(ContextMember(context_id=shared, user_id=users["member"], role="editor"))
        db.add(WorkspaceMember(workspace_id=other_wid, user_id=users["owner"], role="owner"))
        await db.commit()
    monkeypatch.setattr(get_settings(), "oidc_enabled", True)
    monkeypatch.setattr(oidc_limits, "increment_counter", AsyncMock(return_value=1))
    app = FastAPI()
    app.include_router(me_workspaces.router, prefix="/api/v1")

    async def request_db():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = request_db
    fixture = SimpleNamespace(
        factory=factory,
        app=app,
        wid=wid,
        other_wid=other_wid,
        shared=shared,
        private=private,
        foreign=foreign,
        users=users,
        tokens=tokens,
        token_ids=token_ids,
        grant_ids=grant_ids,
        member_ids=member_ids,
        client_id=prefix,
    )
    try:
        yield fixture
    finally:
        async with factory() as db:
            await db.execute(delete(Workspace).where(Workspace.id.in_([wid, other_wid])))
            await db.execute(delete(OIDCClient).where(OIDCClient.client_id == prefix))
            await db.execute(delete(User).where(User.user_id.in_(users.values())))
            await db.commit()


async def request(store, role="owner", *, path=None, context=None, workspace=None):
    if path is None:
        path = f"/api/v1/me/workspaces/{workspace or store.wid}/access"
        if context is not None:
            path += f"?context_id={context}"
    async with AsyncClient(
        transport=ASGITransport(app=store.app), base_url="https://testserver"
    ) as client:
        return await client.get(path, headers={"Authorization": "Bearer " + store.tokens[role]})


async def change(store, model, key, **values):
    async with store.factory() as db:
        row = (
            (await db.execute(select(User).where(User.user_id == key))).scalar_one()
            if model is User
            else await db.get(model, key)
        )
        for name, value in values.items():
            setattr(row, name, value)
        await db.commit()


@pytest.mark.parametrize(
    "role,billing,sync",
    [
        ("owner", True, True),
        ("admin", False, True),
        ("member", False, True),
        ("viewer", False, False),
    ],
)
async def test_roles_and_explicit_workspace_ignore_ui_selection(store, role, billing, sync):
    await change(store, User, store.users[role], current_workspace_id=store.other_wid)
    response = await request(store, role, context=store.shared)
    assert_private(response, 200)
    assert response.json() == {
        "subject": store.users[role],
        "workspace_id": str(store.wid),
        "workspace_role": role,
        "workspace_permissions": {"billing.manage": billing, "usage.read": True},
        "resource": {
            "context_id": str(store.shared),
            "permissions": {"read": True, "sync.manage": sync},
        },
    }


async def test_owner_requires_both_canonical_owner_and_membership_role(store):
    await change(store, WorkspaceMember, store.member_ids["admin"], role="owner")
    assert (await request(store, "admin")).json()["workspace_permissions"][
        "billing.manage"
    ] is False
    await change(store, Workspace, store.wid, owner_user_id=store.users["admin"])
    assert (await request(store, "owner")).json()["workspace_permissions"][
        "billing.manage"
    ] is False
    assert (await request(store, "admin")).json()["workspace_permissions"]["billing.manage"] is True
    await change(store, WorkspaceMember, store.member_ids["admin"], role="admin")
    assert (await request(store, "admin")).json()["workspace_permissions"][
        "billing.manage"
    ] is False


async def test_listing_keyset_pagination_and_current_membership(store):
    path = "/api/v1/me/workspaces?limit=1"
    first = await request(store, path=path)
    assert_private(first, 200)
    assert len(first.json()["workspaces"]) == 1
    cursor = first.json()["next_cursor"]
    second = await request(store, path=path + "&cursor=" + cursor)
    assert_private(second, 200)
    assert second.json()["next_cursor"] is None
    assert (
        first.json()["workspaces"][0]["workspace_id"]
        < second.json()["workspaces"][0]["workspace_id"]
    )
    await change(store, Workspace, store.other_wid, deleted_at=utcnow())
    current = await request(store, path="/api/v1/me/workspaces")
    assert [item["workspace_id"] for item in current.json()["workspaces"]] == [str(store.wid)]
    empty = await request(store, "outsider", path="/api/v1/me/workspaces")
    assert empty.json() == {
        "subject": store.users["outsider"],
        "workspaces": [],
        "next_cursor": None,
    }


async def test_nonmember_system_admin_deleted_and_missing_workspace_are_identical(store):
    for workspace in (store.wid, uuid4()):
        response = await request(store, "outsider", workspace=workspace)
        assert_private(response, 404)
        assert response.json() == {"error": "not_found"}
    await change(store, Workspace, store.wid, deleted_at=utcnow())
    assert_private(await request(store), 404)


async def test_resource_privacy_explicit_workspace_and_deletion(store):
    assert_private(await request(store, "owner", context=store.private), 404)
    assert_private(await request(store, "admin", context=store.private), 404)
    assert_private(await request(store, "member", context=store.private), 200)
    assert_private(await request(store, "owner", context=store.foreign), 404)
    assert_private(await request(store, "owner", context=uuid4()), 404)
    await change(store, Context, store.shared, deleted_at=utcnow())
    assert_private(await request(store, context=store.shared), 404)


async def test_context_role_reach_and_membership_changes_take_effect_on_next_request(store):
    async with store.factory() as db:
        from sqlalchemy import select

        cm = (
            await db.execute(select(ContextMember).where(ContextMember.context_id == store.shared))
        ).scalar_one()
        context_member_id = cm.id
    await change(store, ContextMember, context_member_id, role="viewer")
    response = await request(store, "member", context=store.shared)
    assert response.json()["resource"]["permissions"] == {"read": True, "sync.manage": False}
    await change(store, ContextMember, context_member_id, role="editor")
    assert (await request(store, "member", context=store.shared)).json()["resource"]["permissions"][
        "sync.manage"
    ] is True
    for allowed in ([], None):
        await change(
            store, WorkspaceMember, store.member_ids["member"], allowed_context_ids=allowed
        )
        assert_private(await request(store, "member", context=store.shared), 404)
    await change(
        store, WorkspaceMember, store.member_ids["member"], allowed_context_ids=[store.shared]
    )
    async with store.factory() as db:
        await db.execute(delete(ContextMember).where(ContextMember.id == context_member_id))
        await db.commit()
    assert_private(await request(store, "member", context=store.shared), 404)
    async with store.factory() as db:
        await db.execute(
            delete(WorkspaceMember).where(WorkspaceMember.id == store.member_ids["member"])
        )
        await db.commit()
    assert_private(await request(store, "member"), 404)
    assert (await request(store, "member", path="/api/v1/me/workspaces")).json()["workspaces"] == []


async def test_viewer_cannot_gain_sync_from_explicit_context_role(store):
    async with store.factory() as db:
        db.add(ContextMember(context_id=store.shared, user_id=store.users["viewer"], role="editor"))
        await db.commit()
    assert (await request(store, "viewer", context=store.shared)).json()["resource"]["permissions"][
        "sync.manage"
    ] is False
    await change(store, WorkspaceMember, store.member_ids["viewer"], allowed_context_ids=[])
    assert_private(await request(store, "viewer", context=store.shared), 404)


@pytest.mark.parametrize(
    "kind", ["token", "grant", "client", "access_expiry", "grant_expiry", "user"]
)
async def test_live_credential_revocation_expiry_and_account_removal(store, kind):
    assert_private(await request(store), 200)
    if kind == "token":
        await change(store, OIDCToken, store.token_ids["owner"], revoked_at=utcnow())
    elif kind == "grant":
        await change(store, OIDCGrant, store.grant_ids["owner"], revoked_at=utcnow())
    elif kind == "client":
        await change(store, OIDCClient, store.client_id, revoked_at=utcnow())
    elif kind == "access_expiry":
        await change(store, OIDCToken, store.token_ids["owner"], issued_at=int(time.time()) - 600)
    elif kind == "grant_expiry":
        await change(
            store,
            OIDCGrant,
            store.grant_ids["owner"],
            created_at=utcnow() - timedelta(hours=2),
            expires_at=utcnow() - timedelta(hours=1),
        )
    else:
        async with store.factory() as db:
            await db.execute(delete(User).where(User.user_id == store.users["owner"]))
            await db.commit()
    assert_private(await request(store), 401)


@pytest.mark.parametrize("kind", ["token", "grant", "client"])
async def test_narrowed_authority_never_uses_old_grant_scopes(store, kind):
    model, key = {
        "token": (OIDCToken, store.token_ids["owner"]),
        "grant": (OIDCGrant, store.grant_ids["owner"]),
        "client": (OIDCClient, store.client_id),
    }[kind]
    await change(store, model, key, scope="openid")
    response = await request(store)
    assert_private(response, 403)
    assert 'scope="workspaces:read"' in response.headers["www-authenticate"]


async def test_seat_and_writable_slot_plan_changes_preserve_usage_inspection(store):
    await change(store, Workspace, store.wid, plan_name="free", addon_member_bonus=0)
    suspended = await request(store, "member")
    assert_private(suspended, 200)
    assert suspended.json()["workspace_permissions"] == {
        "billing.manage": False,
        "usage.read": True,
    }
    assert_private(await request(store, "member", context=store.shared), 404)
    first = await request(store, "owner", context=store.shared)
    assert first.json()["resource"]["permissions"] == {"read": True, "sync.manage": True}
    await change(store, Context, store.private, is_private=False)
    later = await request(store, "owner", context=store.private)
    assert later.json()["resource"]["permissions"] == {"read": True, "sync.manage": False}
    await change(store, Workspace, store.wid, plan_name="promax", addon_member_bonus=20)
    assert (await request(store, "member", context=store.shared)).json()["resource"]["permissions"][
        "sync.manage"
    ] is True


async def test_capacity_lock_suppresses_content_permissions_but_preserves_inspection(
    store, monkeypatch
):
    from services.capacity_lock import CapacityLock

    lock = CapacityLock(1001, 1000, 1, 0, 100, 0, "https://example.test/plan")
    monkeypatch.setattr(me_workspaces, "capacity_lock_state", AsyncMock(return_value=lock))
    response = await request(store, context=store.shared)
    assert_private(response, 200)
    assert response.json()["resource"]["permissions"] == {"read": False, "sync.manage": False}
    assert response.json()["workspace_permissions"]["billing.manage"] is True
