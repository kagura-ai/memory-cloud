"""Live ownership, scope, paid window and final-commit gates for sync controls."""

from datetime import UTC, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from models.auth import User, Workspace, WorkspaceMember
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from models.sync_installation import SyncAuthorization, SyncService
from services import sync_installations as service
from tests.integration import sync_installation_fixtures as fixtures
from tests.integration.sync_installation_fixtures import (
    activation,
    approval_body,
    approve,
    change,
    http,
    paid,
    targets,
    transfer,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store


@pytest.mark.parametrize("role", ["admin", "member", "viewer", "outsider", "next"])
async def test_only_current_canonical_owner_can_record_approval(store, role):
    response = await http(store, "POST", "sync-authorizations", role=role, body=approval_body())
    assert response.status_code == 404 and await targets(store) == (0, 0, 0)


async def test_owner_pointer_and_membership_must_both_match(store):
    async with store.factory() as db:
        member = await db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == store.wid,
                WorkspaceMember.user_id == store.users["owner"],
            )
        )
        member.role = "admin"
        await db.commit()
    assert (
        await http(store, "POST", "sync-authorizations", body=approval_body())
    ).status_code == 404


@pytest.mark.parametrize(
    "kind",
    [
        "token_scope",
        "grant_scope",
        "client_scope",
        "token_revoked",
        "grant_revoked",
        "client_revoked",
        "token_expired",
        "grant_expired",
        "unregistered_client",
    ],
)
async def test_live_oidc_authority_is_checked_on_every_control_request(store, kind):
    model, key = (
        (OIDCToken, store.token_ids["owner"])
        if kind.startswith("token")
        else (OIDCGrant, store.grant_ids["owner"])
        if kind.startswith("grant") or kind == "unregistered_client"
        else (OIDCClient, store.client_id)
    )
    if kind.endswith("scope"):
        values, status = {"scope": "openid workspaces:read"}, 403
    elif kind.endswith("revoked"):
        values, status = {"revoked_at": store.clock.now}, 401
    elif kind == "token_expired":
        values, status = (
            {"issued_at": int(store.clock.now.replace(tzinfo=UTC).timestamp()) - 3600},
            401,
        )
    elif kind == "grant_expired":
        # Keep the DB's positive lifetime invariant while expiring the grant.
        values, status = (
            {"created_at": store.clock.now - timedelta(days=1), "expires_at": store.clock.now},
            401,
        )
    else:
        values, status = {"client_id": store.foreign_client}, 409
    await change(store, model, key, **values)
    response = await http(store, "POST", "sync-authorizations", body=approval_body())
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store" and await targets(store) == (0, 0, 0)


@pytest.mark.parametrize(
    "extra",
    ["resource_pk", "context_id", "workspace_id", "service_client_id", "operations", "expires_at"],
)
async def test_owner_cannot_expand_registration_target_operations_or_lifetime(store, extra):
    response = await http(
        store, "POST", "sync-authorizations", body=approval_body(**{extra: "caller-selected"})
    )
    assert response.status_code == 400 and await targets(store) == (0, 0, 0)


@pytest.mark.parametrize(
    "kind",
    [
        "transfer",
        "away_and_back",
        "withdrawal",
        "expiry",
        "registration_generation",
        "service_disabled",
        "browser_revoked",
        "browser_scope",
        "operations",
    ],
)
async def test_pending_consent_is_invalidated_before_paid_internal_activation(store, kind):
    approval = await approve(store)
    await paid(store)
    aid = UUID(approval["authorization_id"])
    if kind in ("transfer", "away_and_back"):
        await transfer(store, "next")
        if kind == "away_and_back":
            await transfer(store, "owner")
    elif kind == "withdrawal":
        assert (await http(store, "DELETE", "sync-authorizations/" + str(aid))).status_code == 200
    elif kind == "expiry":
        store.clock.now += timedelta(days=7)
    elif kind == "registration_generation":
        await change(store, SyncService, store.service_id, generation=2)
    elif kind == "service_disabled":
        await change(store, SyncService, store.service_id, enabled=False)
    elif kind == "browser_revoked":
        await change(store, OIDCClient, store.client_id, revoked_at=store.clock.now)
    elif kind == "browser_scope":
        await change(store, OIDCClient, store.client_id, scope="openid workspaces:read")
    else:
        await change(store, SyncAuthorization, aid, operations=["arbitrary:admin"])
    response = await http(store, "POST", "", billing=True, body=activation(approval))
    assert response.status_code == 409, response.text
    assert await targets(store) == (0, 0, 0)


async def test_fresh_explicit_consent_after_transfer_uses_new_epoch(store):
    old = await approve(store)
    await paid(store)
    await transfer(store, "next")
    fresh = await approve(store, role="next")
    assert fresh["ownership_epoch"] == old["ownership_epoch"] + 1
    result = await http(store, "POST", "", billing=True, body=activation(fresh))
    assert result.status_code == 200, result.text


@pytest.mark.parametrize("kind", ["missing", "no_service", "no_max", "expired", "tier"])
async def test_only_live_confirmed_max_and_service_rights_allow_provisioning(store, kind):
    approval = await approve(store)
    if kind != "missing":
        await paid(
            store,
            services=kind != "no_service",
            max_access=kind != "no_max",
            end=store.clock.now if kind == "expired" else None,
        )
    if kind == "tier":
        await change(store, Workspace, store.wid, plan_name="pro")
    result = await http(store, "POST", "", billing=True, body=activation(approval))
    assert result.status_code == 404, result.text
    assert await targets(store) == (0, 0, 0)


@pytest.mark.parametrize("kind", ["approval", "human_token", "paid_term"])
async def test_expiry_during_provisioning_rolls_back_at_final_commit(store, monkeypatch, kind):
    approval = await approve(store)
    start = store.clock.now
    await paid(store, end=start + timedelta(seconds=1) if kind == "paid_term" else None)
    original = service.create_sync_target

    async def advance_after_target(*args):
        result = await original(*args)
        store.clock.now = start + (
            timedelta(days=7)
            if kind == "approval"
            else timedelta(hours=1)
            if kind == "human_token"
            else timedelta(seconds=1)
        )
        return result

    monkeypatch.setattr(service, "create_sync_target", advance_after_target)
    response = await http(
        store,
        "POST",
        "sync-installations",
        billing=kind != "human_token",
        body=activation(approval),
    )
    assert response.status_code == {"approval": 409, "human_token": 401, "paid_term": 404}[kind], (
        response.text
    )
    assert await targets(store) == (0, 0, 0)
    async with store.factory() as db:
        assert (await db.get(SyncAuthorization, UUID(approval["authorization_id"]))).used_at is None


async def test_partial_failure_is_nonreflective_and_retryable(store, monkeypatch):
    approval = await approve(store)
    await paid(store)
    original = service.create_sync_target

    async def fail_after_target(*args):
        await original(*args)
        raise RuntimeError("private-error-details-must-not-leak")

    monkeypatch.setattr(service, "create_sync_target", fail_after_target)
    response = await http(store, "POST", "", billing=True, body=activation(approval))
    assert response.status_code == 503 and "private-error" not in response.text
    assert await targets(store) == (0, 0, 0)
    monkeypatch.setattr(service, "create_sync_target", original)
    assert (
        await http(store, "POST", "", billing=True, body=activation(approval))
    ).status_code == 200
    assert await targets(store) == (1, 1, 1)


async def test_application_and_cross_workspace_substitutions_are_refused(store):
    approval = await approve(store)
    await paid(store)
    assert (
        await http(
            store,
            "POST",
            "",
            billing=True,
            body={**activation(approval), "application_id": "different"},
        )
    ).status_code == 409
    assert (
        await http(
            store, "POST", "", billing=True, body=activation(approval), workspace=store.other
        )
    ).status_code == 404
    assert (
        await http(
            store,
            "POST",
            "",
            billing=True,
            body={**activation(approval), "resource_pk": str(store.other)},
        )
    ).status_code == 400
    assert await targets(store) == (0, 0, 0)


async def test_original_owner_account_deletion_clears_identity_but_retains_active_binding(store):
    approval = await approve(store)
    await paid(store)
    result = (await http(store, "POST", "", billing=True, body=activation(approval))).json()
    await transfer(store, "next")
    async with store.factory() as db:
        await db.delete(await db.scalar(select(User).where(User.user_id == store.users["owner"])))
        await db.commit()
    async with store.factory() as db:
        assert (
            await db.get(SyncAuthorization, UUID(approval["authorization_id"]))
        ).authorized_by is None
    viewed = await http(
        store, "GET", "sync-installations/" + result["installation_id"], role="next"
    )
    assert viewed.status_code == 200 and viewed.json()["target_available"]
    assert (await http(store, "POST", "", billing=True, body=activation(approval))).json()[
        "installation_id"
    ] == result["installation_id"]
