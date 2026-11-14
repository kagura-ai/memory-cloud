"""Fresh-session HTTP/PG fixtures for the #2802 control-plane contract."""

import secrets
from datetime import UTC, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes import internal_sync_installations, sync_installations
from auth import oidc_limits, sync_identity
from auth.oidc_server import credential_hash
from config.settings import get_settings
from db.base import get_db
from models.auth import Context, User, Workspace, WorkspaceMember
from models.integration_entitlement_schemas import IntegrationEntitlementPush
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from models.sync_installation import SyncService
from services import integration_entitlements, sync_authority
from services import sync_installations as service
from services.workspace_ownership_service import WorkspaceOwnershipService
from utils.datetime import utcnow


@pytest_asyncio.fixture(loop_scope="session")
async def store(async_engine, monkeypatch):
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    prefix = "sync-" + uuid4().hex
    users = {
        r: prefix + "-" + r for r in ("owner", "next", "admin", "member", "viewer", "outsider")
    }
    wid, other = uuid4(), uuid4()
    clock = SimpleNamespace(now=utcnow().replace(microsecond=0))
    tokens, token_ids, grant_ids = {}, {}, {}
    client_id, foreign_client = prefix + "-browser", prefix + "-foreign"
    async with factory() as db:
        db.add_all(
            [
                User(
                    user_id=u,
                    email=u + "@example.test",
                    role="admin" if r == "outsider" else "user",
                )
                for r, u in users.items()
            ]
        )
        await db.flush()
        db.add_all(
            [
                Workspace(
                    id=wid,
                    name="Sync workspace",
                    owner_user_id=users["owner"],
                    plan_name="promax",
                    addon_member_bonus=20,
                ),
                Workspace(
                    id=other,
                    name="Other workspace",
                    owner_user_id=users["owner"],
                    plan_name="promax",
                ),
                *[
                    OIDCClient(
                        client_id=c,
                        client_name="Sync controls fixture",
                        client_secret_hash="not-used",
                        redirect_uris=["https://client.example.test/callback"],
                        scope="openid workspaces:read integrations:manage",
                    )
                    for c in (client_id, foreign_client)
                ],
            ]
        )
        await db.flush()
        db.add(
            SyncService(
                service_id=prefix, browser_client_id=client_id, service_client_id=prefix + "-worker"
            )
        )
        for role, uid in users.items():
            if role != "outsider":
                db.add(
                    WorkspaceMember(
                        workspace_id=wid, user_id=uid, role="member" if role == "next" else role
                    )
                )
            grant = OIDCGrant(
                client_id=client_id,
                user_id=uid,
                scope="openid workspaces:read integrations:manage",
                nonce=uuid4().hex,
                auth_time=int(clock.now.replace(tzinfo=UTC).timestamp()),
                expires_at=clock.now + timedelta(hours=8),
            )
            db.add(grant)
            await db.flush()
            token = "kmc_oidc_at_" + secrets.token_urlsafe(32)
            row = OIDCToken(
                grant_id=grant.id,
                access_token_hash=credential_hash(token),
                refresh_token_hash=credential_hash(secrets.token_urlsafe(32)),
                scope=grant.scope,
                issued_at=grant.auth_time,
                expires_in=3600,
            )
            db.add(row)
            await db.flush()
            tokens[role], token_ids[role], grant_ids[role] = token, row.id, grant.id
        db.add(WorkspaceMember(workspace_id=other, user_id=users["owner"], role="owner"))
        await db.commit()
    settings = get_settings()
    for name, value in {
        "oidc_enabled": True,
        "sync_integrations_enabled": True,
        "sync_authorization_ttl_seconds": 604800,
        "mcp_public_endpoints_enabled": True,
        "billing_service_token": "fixture-billing-2802",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(oidc_limits, "increment_counter", AsyncMock(return_value=1))
    for module in (service, sync_authority, sync_identity, integration_entitlements):
        monkeypatch.setattr(module, "utcnow", lambda: clock.now)
    app = FastAPI()
    app.include_router(sync_installations.router, prefix="/api/v1")
    app.include_router(internal_sync_installations.router)

    async def sessions():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = sessions
    try:
        yield SimpleNamespace(
            factory=factory,
            app=app,
            users=users,
            wid=wid,
            other=other,
            clock=clock,
            client_id=client_id,
            foreign_client=foreign_client,
            service_id=prefix,
            tokens=tokens,
            token_ids=token_ids,
            grant_ids=grant_ids,
        )
    finally:
        async with factory() as db:
            await db.execute(delete(Workspace).where(Workspace.id.in_([wid, other])))
            await db.execute(delete(SyncService).where(SyncService.service_id == prefix))
            await db.execute(
                delete(OIDCClient).where(OIDCClient.client_id.in_([client_id, foreign_client]))
            )
            await db.execute(delete(User).where(User.user_id.in_(list(users.values()))))
            await db.commit()


async def http(
    store, method, suffix, *, body=None, role="owner", billing=False, workspace=None, validate=False
):
    wid = workspace or store.wid
    path = (
        f"/internal/workspaces/{wid}/"
        + ("sync-authorizations/validate" if validate else "sync-installations/activate")
        if billing
        else f"/api/v1/workspaces/{wid}/{suffix}"
    )
    token = "fixture-billing-2802" if billing else store.tokens[role]
    async with AsyncClient(
        transport=ASGITransport(app=store.app), base_url="https://test"
    ) as client:
        return await client.request(
            method, path, json=body, headers={"Authorization": "Bearer " + token}
        )


def approval_body(**extra):
    return {
        "request_id": str(uuid4()),
        "application_id": "application-1",
        "public_name": "Approved catalog",
        "public_description": "Approved public text",
        **extra,
    }


async def approve(store, body=None, role="owner"):
    response = await http(
        store, "POST", "sync-authorizations", body=body or approval_body(), role=role
    )
    assert response.status_code == 201, response.text
    return response.json()


def activation(approval):
    return {
        "authorization_id": approval["authorization_id"],
        "application_id": approval["application_id"],
    }


async def paid(store, revision=1, *, services=True, max_access=True, end=None):
    window = {
        "valid_from": (store.clock.now - timedelta(hours=1)).replace(tzinfo=UTC),
        "valid_until": (end or store.clock.now + timedelta(days=14)).replace(tzinfo=UTC),
        "evidence_ref": "confirmed-term",
    }
    body = IntegrationEntitlementPush(
        revision=revision,
        event_id=f"paid-{revision}",
        source_ref="billing-snapshot",
        max_access=window if max_access else None,
        services=[{"service_id": store.service_id, **window}] if services else [],
    )
    async with store.factory() as db:
        await integration_entitlements.apply_integration_entitlements(db, store.wid, body)
        await db.commit()


async def targets(store):
    async with store.factory() as db:
        return tuple(
            [
                await db.scalar(
                    select(func.count()).select_from(model).where(model.workspace_id == store.wid)
                )
                for model in (Resource, Context, PublicMcpEndpoint)
            ]
        )


async def change(store, model, key, **values):
    async with store.factory() as db:
        row = await db.get(model, key)
        for name, value in values.items():
            setattr(row, name, value)
        await db.commit()


async def transfer(store, role):
    async with store.factory() as db:
        owner_id = await db.scalar(select(Workspace.owner_user_id).where(Workspace.id == store.wid))
        return await WorkspaceOwnershipService(db).transfer_ownership(
            workspace_id=store.wid,
            current_owner_id=owner_id,
            target_user_id=store.users[role],
            performed_by_email="fixture@example.test",
        )
