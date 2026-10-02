"""Opaque public ids on the REST surface, against a real database (#1008).

For API keys, share keys, resource tokens, workspace invitations and member
credential keys:

- responses carry the ``<prefix>_...`` public id, never the integer PK;
- an integer path id is a 422 from the path pattern (hard cut, no
  dual-accept period);
- another owner's well-formed id gets the same 404 as an id that does not
  exist, so a public id is no existence oracle either;
- the caller's own public id works.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from auth.dependencies import (
    get_current_user,
    get_user_from_api_key_or_session,
    require_session_auth,
    require_workspace_owner,
)
from auth.resource_tokens import ResourceTokenManager
from auth.workspace_roles import WorkspaceRole
from db.base import get_db
from models.auth import (
    APIKey,
    AuditLog,
    Context,
    ShareKey,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.resource import Resource, ResourceToken
from utils.datetime import utcnow
from utils.public_id import PublicIdPrefix, new_public_id, public_id_pattern

# Fields of an error body that legitimately differ between two requests.
_VOLATILE = {"request_id", "correlation_id", "timestamp", "trace_id"}


def _stable(body: Any) -> Any:
    if isinstance(body, dict):
        return {k: _stable(v) for k, v in body.items() if k not in _VOLATILE}
    return body


def _session_override(engine):
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with maker() as session:
            try:
                yield session
            finally:
                await session.rollback()

    return override_get_db


async def _seed_owner(db: AsyncSession, label: str) -> dict[str, Any]:
    suffix = uuid4().hex[:10]
    user_id = f"pid-{label}-{suffix}"
    ws = Workspace(
        id=uuid4(),
        name=f"ws-{label}-{suffix}",
        plan_name="pro",
        owner_user_id=user_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    db.add(ws)
    await db.flush()
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{label}-{suffix}",
        resource_id=f"res-{label}-{suffix}",
        created_by=user_id,
    )
    resource = Resource(
        id=uuid4(), workspace_id=ws.id, resource_id=ctx.resource_id, created_by=user_id
    )
    db.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=user_id, role=WorkspaceRole.OWNER),
            ctx,
            resource,
        ]
    )
    await db.flush()
    api_key = APIKey(
        key_hash=uuid4().hex.ljust(64, "a"),
        key_prefix="kagura_pidtest00",
        name=f"key-{label}",
        user_id=user_id,
        workspace_id=ws.id,
    )
    share_key = ShareKey(
        key_hash=uuid4().hex.ljust(64, "b"),
        key_prefix="kagura_pidshare0",
        name=f"share-{label}",
        user_id=user_id,
        context_id=ctx.id,
        expires_at=utcnow() + timedelta(days=7),
    )
    token = ResourceToken(
        resource_pk=resource.id,
        resource_id=ctx.resource_id,
        workspace_id=ws.id,
        token_hash=f"pid-{uuid4().hex}",
        quota_events_per_hour=100,
        created_by=user_id,
        is_active=True,
    )
    invitation = WorkspaceInvitation(
        workspace_id=ws.id,
        token=f"pid-invite-{uuid4().hex}",
        email=f"invitee-{suffix}@test.example",
        role=WorkspaceRole.MEMBER,
        invited_by=user_id,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add_all([api_key, share_key, token, invitation])
    await db.flush()
    return {
        "user_id": user_id,
        "workspace": ws,
        "context": ctx,
        "api_key": api_key,
        "share_key": share_key,
        "token": token,
        "invitation": invitation,
    }


@pytest_asyncio.fixture
async def owners(async_engine, db_session: AsyncSession) -> AsyncIterator[dict[str, Any]]:
    a = await _seed_owner(db_session, "a")
    b = await _seed_owner(db_session, "b")
    await db_session.commit()

    user_a = {
        "user_id": a["user_id"],
        "sub": a["user_id"],
        "email": f"{a['user_id']}@test.example",
        "role": "user",
        "current_workspace_id": a["workspace"].id,
        "workspace_role": "owner",
    }

    async def _user() -> dict:
        return user_a

    async def _owner() -> tuple:
        return (a["user_id"], a["workspace"].id)

    app.dependency_overrides[get_db] = _session_override(async_engine)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_session_auth] = _user
    app.dependency_overrides[get_user_from_api_key_or_session] = _user
    app.dependency_overrides[require_workspace_owner] = _owner
    try:
        yield {"a": a, "b": b}
    finally:
        app.dependency_overrides.clear()
        for owner in (a, b):
            ws_id = owner["workspace"].id
            for model, col in (
                (APIKey, APIKey.user_id),
                (ShareKey, ShareKey.user_id),
                (ResourceToken, ResourceToken.created_by),
                (AuditLog, AuditLog.user_id),
            ):
                await db_session.execute(model.__table__.delete().where(col == owner["user_id"]))
            await db_session.execute(
                WorkspaceInvitation.__table__.delete().where(
                    WorkspaceInvitation.workspace_id == ws_id
                )
            )
            await db_session.execute(
                Context.__table__.delete().where(Context.workspace_id == ws_id)
            )
            await db_session.execute(
                Resource.__table__.delete().where(Resource.workspace_id == ws_id)
            )
            await db_session.execute(
                WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id == ws_id)
            )
            await db_session.execute(Workspace.__table__.delete().where(Workspace.id == ws_id))
        await db_session.commit()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _assert_public(value: Any, prefix: PublicIdPrefix) -> None:
    assert isinstance(value, str), value
    assert re.fullmatch(public_id_pattern(prefix), value), value


def _assert_uniform_404(client: TestClient, method: str, other: str, unknown: str) -> None:
    r_other = client.request(method, other)
    r_unknown = client.request(method, unknown)
    assert r_other.status_code == 404, r_other.text
    assert r_unknown.status_code == 404, r_unknown.text
    assert _stable(r_other.json()) == _stable(r_unknown.json())


async def _reload(db: AsyncSession, row: Any) -> Any:
    await db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# API keys (/api/v1/config/api-keys)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_api_keys_use_public_ids(owners, client: TestClient, db_session) -> None:
    a, b = owners["a"], owners["b"]
    mine = a["api_key"].public_id
    unknown = new_public_id(PublicIdPrefix.API_KEY)
    base = "/api/v1/config/api-keys"

    listed = client.get(base)
    assert listed.status_code == 200, listed.text
    ids = [k["id"] for k in listed.json()]
    for pid in ids:
        _assert_public(pid, PublicIdPrefix.API_KEY)
    assert mine in ids
    assert b["api_key"].public_id not in ids

    for method, suffix in (
        ("GET", "/stats"),
        ("POST", "/revoke"),
        ("POST", "/regenerate"),
        ("DELETE", ""),
    ):
        assert client.request(method, f"{base}/{a['api_key'].id}{suffix}").status_code == 422
        _assert_uniform_404(
            client,
            method,
            f"{base}/{b['api_key'].public_id}{suffix}",
            f"{base}/{unknown}{suffix}",
        )

    assert client.get(f"{base}/{mine}/stats").status_code == 200

    regen = client.post(f"{base}/{mine}/regenerate")
    assert regen.status_code == 200, regen.text
    new_id = regen.json()["id"]
    _assert_public(new_id, PublicIdPrefix.API_KEY)
    assert new_id != mine

    assert client.post(f"{base}/{new_id}/revoke").status_code == 204
    assert client.delete(f"{base}/{new_id}").status_code == 204


# ---------------------------------------------------------------------------
# Share keys (/api/v1/config/share-keys)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_share_keys_use_public_ids(owners, client: TestClient) -> None:
    a, b = owners["a"], owners["b"]
    base = "/api/v1/config/share-keys"

    listed = client.get(base)
    assert listed.status_code == 200, listed.text
    ids = [k["id"] for k in listed.json()]
    for pid in ids:
        _assert_public(pid, PublicIdPrefix.SHARE_KEY)
    assert ids == [a["share_key"].public_id]

    assert client.post(f"{base}/{a['share_key'].id}/revoke").status_code == 422
    _assert_uniform_404(
        client,
        "POST",
        f"{base}/{b['share_key'].public_id}/revoke",
        f"{base}/{new_public_id(PublicIdPrefix.SHARE_KEY)}/revoke",
    )
    assert client.post(f"{base}/{a['share_key'].public_id}/revoke").status_code == 204


# ---------------------------------------------------------------------------
# Resource tokens (/api/v1/resource-tokens)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_resource_tokens_use_public_ids(owners, client: TestClient) -> None:
    a, b = owners["a"], owners["b"]
    base = "/api/v1/resource-tokens"
    mine = a["token"].public_id
    unknown = new_public_id(PublicIdPrefix.RESOURCE_TOKEN)

    listed = client.get(base, params={"resource_id": a["context"].resource_id})
    assert listed.status_code == 200, listed.text
    ids = [t["id"] for t in listed.json()["tokens"]]
    assert ids == [mine]
    _assert_public(ids[0], PublicIdPrefix.RESOURCE_TOKEN)

    body = {"description": "renamed"}
    assert client.patch(f"{base}/{a['token'].id}", json=body).status_code == 422
    assert client.delete(f"{base}/{a['token'].id}").status_code == 422

    r_other = client.patch(f"{base}/{b['token'].public_id}", json=body)
    r_unknown = client.patch(f"{base}/{unknown}", json=body)
    assert r_other.status_code == r_unknown.status_code == 404
    assert _stable(r_other.json()) == _stable(r_unknown.json())
    _assert_uniform_404(client, "DELETE", f"{base}/{b['token'].public_id}", f"{base}/{unknown}")

    patched = client.patch(f"{base}/{mine}", json=body)
    assert patched.status_code == 200, patched.text
    assert patched.json()["id"] == mine
    assert client.delete(f"{base}/{mine}").status_code == 204


# ---------------------------------------------------------------------------
# Workspace invitations (/api/v1/workspaces/{ws}/invitations)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_invitations_use_public_ids(owners, client: TestClient, db_session) -> None:
    a, b = owners["a"], owners["b"]
    ws_a = a["workspace"].id
    base = f"/api/v1/workspaces/{ws_a}/invitations"
    mine = a["invitation"].public_id

    listed = client.get(base)
    assert listed.status_code == 200, listed.text
    ids = [i["id"] for i in listed.json()]
    assert ids == [mine]
    _assert_public(ids[0], PublicIdPrefix.INVITATION)

    assert client.delete(f"{base}/{a['invitation'].id}").status_code == 422
    # B's invitation addressed through A's workspace: same 404 as unknown.
    _assert_uniform_404(
        client,
        "DELETE",
        f"{base}/{b['invitation'].public_id}",
        f"{base}/{new_public_id(PublicIdPrefix.INVITATION)}",
    )
    assert client.delete(f"{base}/{mine}").status_code == 200
    gone = await db_session.execute(
        select(WorkspaceInvitation).where(WorkspaceInvitation.public_id == mine)
    )
    assert gone.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Member credentials (/api/v1/workspaces/{ws}/members/{user}/credentials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_member_credential_keys_use_public_ids(owners, client: TestClient) -> None:
    a, b = owners["a"], owners["b"]
    ws_a = a["workspace"].id
    base = f"/api/v1/workspaces/{ws_a}/members/{a['user_id']}/credentials"
    mine = a["api_key"].public_id

    creds = client.get(base)
    assert creds.status_code == 200, creds.text
    ids = [k["id"] for k in creds.json()["api_keys"]]
    assert ids == [mine]
    _assert_public(ids[0], PublicIdPrefix.API_KEY)

    assert client.delete(f"{base}/api-keys/{a['api_key'].id}").status_code == 422
    _assert_uniform_404(
        client,
        "DELETE",
        f"{base}/api-keys/{b['api_key'].public_id}",
        f"{base}/api-keys/{new_public_id(PublicIdPrefix.API_KEY)}",
    )

    deleted = client.delete(f"{base}/api-keys/{mine}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted", "key_id": mine}


# ---------------------------------------------------------------------------
# Create responses carry the public id the database issued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_create_responses_use_public_ids(owners, client: TestClient, db_session) -> None:
    a = owners["a"]

    share = client.post(
        "/api/v1/config/share-keys",
        json={"name": "pid-create", "context_id": str(a["context"].id)},
    )
    assert share.status_code == 201, share.text
    share_id = share.json()["id"]
    _assert_public(share_id, PublicIdPrefix.SHARE_KEY)
    stored_share = await db_session.execute(
        select(ShareKey.public_id).where(ShareKey.public_id == share_id)
    )
    assert stored_share.scalar_one() == share_id

    # Resource tokens are an XL (promax) feature.
    await db_session.execute(
        Workspace.__table__.update()
        .where(Workspace.id == a["workspace"].id)
        .values(plan_name="promax")
    )
    await db_session.commit()
    token = client.post(
        "/api/v1/resource-tokens",
        json={"resource_id": a["context"].resource_id, "description": "pid-create"},
    )
    assert token.status_code == 201, token.text
    token_id = token.json()["id"]
    _assert_public(token_id, PublicIdPrefix.RESOURCE_TOKEN)
    stored_token = await db_session.execute(
        select(ResourceToken.public_id).where(ResourceToken.public_id == token_id)
    )
    assert stored_token.scalar_one() == token_id


@pytest.mark.asyncio(loop_scope="session")
async def test_resource_token_revoke_race_hides_integer_id(
    owners, client: TestClient, monkeypatch
) -> None:
    """A token that vanishes between lookup and revoke gets the uniform 404.

    The manager's ``ValueError`` names the integer PK; the response must not.
    """
    a = owners["a"]
    pk = a["token"].id

    async def _vanished(self, token_id: int) -> None:
        raise ValueError(f"Resource token {token_id} not found")

    monkeypatch.setattr(ResourceTokenManager, "revoke_token", _vanished)
    resp = client.delete(f"/api/v1/resource-tokens/{a['token'].public_id}")
    assert resp.status_code == 404, resp.text
    assert "Resource token not found" in resp.text, resp.text
    assert f"Resource token {pk} not found" not in resp.text
