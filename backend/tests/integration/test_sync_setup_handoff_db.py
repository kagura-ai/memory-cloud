"""Actual owner/registration/COMMIT fences for the setup-only credential (#2878)."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from joserfc import jwt
from joserfc.jwk import OKPKey
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from auth import sync_setup_handoff as signer_module
from config.settings import get_settings
from models.auth import AuditLog, User, WorkspaceMember
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from models.sync_installation import SyncAuthorization, SyncInstallation, SyncService
from services import sync_setup_handoff as service
from tests.auth._billing_handoff_helpers import ed25519_keypair
from tests.integration import sync_installation_fixtures as fixtures
from tests.integration.sync_installation_fixtures import (
    approval_body,
    change,
    http,
    targets,
    transfer,
)
from utils.exceptions import ConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store
CONTRACT = json.loads(
    (Path(__file__).parents[1] / "fixtures/sync-setup-handoff-v1.json").read_text()
)


@pytest.fixture
def setup(store, monkeypatch):
    private, public = ed25519_keypair()
    settings = get_settings()
    for name, value in {
        "billing_handoff_signing_key": private,
        "billing_handoff_key_id": "fixture-setup-key",
        "billing_handoff_issuer": "kagura-memory-cloud",
        "billing_handoff_audience": "kagura-billing",
        "billing_handoff_ttl_seconds": 120,
        "payment_public_base_url": "https://payment.example.test",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(signer_module, "utcnow", lambda: store.clock.now)
    store.setup_public_key = public
    store.setup_body = {
        "application_id": str(uuid4()),
        "application_revision": CONTRACT["request"]["application_revision"],
        "input_digest": CONTRACT["request"]["input_digest"],
    }
    return store


async def request(setup, *, role="owner"):
    return await http(setup, "POST", "sync-billing-handoffs", role=role, body=setup.setup_body)


def claims(setup, response):
    return jwt.decode(
        response.json()["token"],
        OKPKey.import_key(setup.setup_public_key),
        algorithms=["Ed25519"],
    ).claims


async def audit_count(setup):
    async with setup.factory() as db:
        return await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "sync.setup_handoff.created",
                AuditLog.user_metadata["workspace_id"].as_string() == str(setup.wid),
            )
        )


async def test_current_registered_owner_gets_only_exact_setup_context(setup):
    response = await request(setup)
    assert response.status_code == CONTRACT["response"]["status"]
    assert response.headers["cache-control"] == "no-store"
    data, signed = response.json(), claims(setup, response)
    assert data["contract_version"] == CONTRACT["contract_version"]
    assert data["token_type"] == CONTRACT["response"]["token_type"]
    assert set(data) == set(CONTRACT["response"]["fields"])
    assert signed["aud"] == CONTRACT["token"]["audience"]
    assert signed["purpose"] == CONTRACT["token"]["purpose"]
    assert set(signed) == set(CONTRACT["token"]["required_claims"])
    assert not set(signed).intersection(CONTRACT["token"]["forbidden_claims"])
    assert signed["sub"] == setup.users["owner"]
    assert signed["workspace_id"] == str(setup.wid)
    assert signed["service_id"] == setup.service_id
    assert signed["client_id"] == setup.client_id
    for name in ("application_id", "application_revision", "input_digest"):
        assert signed[name] == setup.setup_body[name]
    assert signed["epoch"] == 0 and signed["exp"] - signed["iat"] == 60
    location = urlsplit(data["url"])
    assert location.scheme == "https" and location.netloc == "payment.example.test"
    assert location.path == CONTRACT["landing"]["path"]
    assert parse_qs(location.query) == {"t": [data["token"]]}
    assert await targets(setup) == (0, 0, 0)
    async with setup.factory() as db:
        for model in (SyncAuthorization, SyncInstallation):
            assert (
                await db.scalar(
                    select(func.count()).select_from(model).where(model.workspace_id == setup.wid)
                )
                == 0
            )
        audit = await db.scalar(
            select(AuditLog).where(
                AuditLog.action == "sync.setup_handoff.created",
                AuditLog.user_metadata["workspace_id"].as_string() == str(setup.wid),
            )
        )
        assert audit is not None
        metadata = json.dumps(audit.user_metadata)
        assert data["token"] not in metadata
        assert setup.setup_body["input_digest"] not in metadata
        assert "sync-service:" + setup.service_id == audit.resource


@pytest.mark.parametrize("role", ["admin", "member", "viewer", "outsider", "next"])
async def test_other_roles_cannot_inherit_canonical_owner_authority(setup, role):
    response = await request(setup, role=role)
    assert response.status_code == 404
    assert await audit_count(setup) == 0 and await targets(setup) == (0, 0, 0)


async def test_canonical_pointer_alone_is_not_owner_membership(setup):
    async with setup.factory() as db:
        member = await db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == setup.wid,
                WorkspaceMember.user_id == setup.users["owner"],
            )
        )
        member.role = "admin"
        await db.commit()
    assert (await request(setup)).status_code == 404
    assert await audit_count(setup) == 0


@pytest.mark.parametrize("kind", ["token", "grant", "client"])
async def test_revoked_identity_is_rechecked_in_actual_sql(setup, kind):
    model, key = {
        "token": (OIDCToken, setup.token_ids["owner"]),
        "grant": (OIDCGrant, setup.grant_ids["owner"]),
        "client": (OIDCClient, setup.client_id),
    }[kind]
    await change(setup, model, key, revoked_at=setup.clock.now)
    assert (await request(setup)).status_code == 401
    assert await audit_count(setup) == 0


async def test_ordinary_login_scope_cannot_open_payment_setup(setup):
    await change(setup, OIDCToken, setup.token_ids["owner"], scope="openid profile workspaces:read")
    assert (await request(setup)).status_code == 403
    assert await audit_count(setup) == 0


@pytest.mark.parametrize("kind", ["access", "grant"])
async def test_original_identity_expiry_cannot_be_renewed_by_setup_mint(setup, kind):
    if kind == "access":
        await change(setup, OIDCToken, setup.token_ids["owner"], expires_in=1)
        setup.clock.now += timedelta(seconds=2)
    else:
        async with setup.factory() as db:
            grant = await db.get(OIDCGrant, setup.grant_ids["owner"])
            assert grant is not None
            grant.expires_at = grant.created_at + timedelta(seconds=60)
            setup.clock.now = grant.expires_at + timedelta(seconds=1)
            await db.commit()
    assert (await request(setup)).status_code == 401
    assert await audit_count(setup) == 0


@pytest.mark.parametrize("kind", ["foreign_client", "disabled_service"])
async def test_registration_is_current_and_selected_by_authenticated_client(setup, kind):
    if kind == "foreign_client":
        await change(setup, OIDCGrant, setup.grant_ids["owner"], client_id=setup.foreign_client)
    else:
        await change(setup, SyncService, setup.service_id, enabled=False)
    assert (await request(setup)).status_code == 409
    assert await audit_count(setup) == 0


async def test_owner_transfer_and_return_never_rewrite_old_credential_epoch(setup):
    first = await request(setup)
    assert first.status_code == 200
    old = claims(setup, first)
    await transfer(setup, "next")
    assert (await request(setup)).status_code == 404
    await transfer(setup, "owner")
    fresh = await request(setup)
    assert fresh.status_code == 200
    current = claims(setup, fresh)
    assert old["epoch"] == 0 and current["epoch"] == 2
    assert current["sub"] == old["sub"]
    assert current["jti"] != old["jti"]
    # Payment must compare the original saved epoch to current KMC consent;
    # obtaining a fresh setup credential does not update old billing consent.
    assert claims(setup, first)["epoch"] == 0


async def test_expiry_at_final_commit_rolls_back_audit_and_returns_no_token(setup, monkeypatch):
    original = service.SyncSetupHandoffSigner.mint

    def delayed(signer, **values):
        result = original(signer, **values)
        setup.clock.now += timedelta(seconds=61)
        return result

    monkeypatch.setattr(service.SyncSetupHandoffSigner, "mint", delayed)
    response = await request(setup)
    assert response.status_code == 409
    assert "token" not in response.json()
    assert await audit_count(setup) == 0


@pytest.mark.parametrize(("elapsed", "expected"), [(61, 409), (3601, 401)])
async def test_expiry_during_pending_audit_flush_cannot_return_success(setup, elapsed, expected):
    def expire_after_sql(session, _context):
        if any(
            isinstance(row, AuditLog) and row.action == "sync.setup_handoff.created"
            for row in session.new
        ):
            setup.clock.now += timedelta(seconds=elapsed)

    event.listen(Session, "after_flush", expire_after_sql)
    try:
        response = await request(setup)
    finally:
        event.remove(Session, "after_flush", expire_after_sql)
    assert response.status_code == expected
    assert "token" not in response.json()
    assert await audit_count(setup) == 0


async def test_busy_erasure_owner_is_retryable_without_minting(setup):
    async with setup.factory() as erasure:
        await erasure.scalar(
            select(User).where(User.user_id == setup.users["owner"]).with_for_update()
        )
        response = await asyncio.wait_for(request(setup), 5)
        assert response.status_code == 409
    assert await audit_count(setup) == 0


@pytest.mark.parametrize("operation", ["setup", "existing_authorization"])
async def test_busy_owner_membership_returns_retryable_conflict(setup, operation):
    async with setup.factory() as membership_update:
        await membership_update.scalar(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == setup.wid,
                WorkspaceMember.user_id == setup.users["owner"],
            )
            .with_for_update()
        )
        if operation == "setup":
            response = await asyncio.wait_for(request(setup), 5)
        else:
            response = await asyncio.wait_for(
                http(setup, "POST", "sync-authorizations", body=approval_body()), 5
            )
        assert response.status_code == 409
    assert await audit_count(setup) == 0
    assert await targets(setup) == (0, 0, 0)


async def test_busy_service_registration_is_retryable_without_minting(setup):
    async with setup.factory() as operator:
        await operator.scalar(
            select(SyncService).where(SyncService.service_id == setup.service_id).with_for_update()
        )
        response = await asyncio.wait_for(request(setup), 5)
        assert response.status_code == 409
    assert await audit_count(setup) == 0


async def test_transfer_cannot_pass_the_held_issuance_fence(setup, monkeypatch):
    original = service.registered_service
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(service, "registered_service", paused)
    task = asyncio.create_task(request(setup))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ConflictError):
            await asyncio.wait_for(transfer(setup, "next"), 5)
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.status_code == 200 and claims(setup, result)["epoch"] == 0
        await transfer(setup, "next")
        assert (await request(setup)).status_code == 404
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "http://payment.example.test",
        "https://user:pass@payment.example.test",
        "https://payment.example.test/path",
        "https://payment.example.test/?next=elsewhere",
        "https://payment.example.test/#fragment",
        "https://payment.example.test:0",
        "https://payment.example.test\\@elsewhere.example.test",
    ],
)
async def test_misconfigured_landing_never_emits_a_credential(setup, monkeypatch, origin):
    monkeypatch.setattr(get_settings(), "payment_public_base_url", origin)
    response = await request(setup)
    assert response.status_code == 503 and "token" not in response.json()
    assert await audit_count(setup) == 0
