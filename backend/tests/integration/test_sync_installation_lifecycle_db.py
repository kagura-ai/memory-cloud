"""Consent, paid provisioning, stable targets and owner-independent lifetime (#2802)."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from mcp_server.public.publication import load_serving_endpoint
from models.auth import AuditLog, Context, WorkspaceMember
from models.config import ContextSearchConfig
from models.oidc import OIDCGrant, OIDCToken
from models.public_mcp import PublicMcpEndpoint
from models.resource import ResourceToken
from models.sync_installation import SyncInstallation
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


async def test_recorded_approval_is_not_provisioning_and_retry_is_not_renewal(store):
    body = approval_body()
    first = await approve(store, body)
    replay = await approve(store, body)
    assert first == replay and await targets(store) == (0, 0, 0)
    bad = await http(store, "POST", "sync-authorizations", body={**body, "public_name": "Changed"})
    assert bad.status_code == 409
    await http(store, "DELETE", "sync-authorizations/" + first["authorization_id"])
    replay = await approve(store, body)
    assert replay["revoked_at"] is not None
    assert replay["expires_at"] == first["expires_at"]
    renewed = await approve(store)
    assert renewed["authorization_id"] != first["authorization_id"]


async def test_billing_validation_before_payment_reserves_no_future_authority(store):
    approval = await approve(store)
    checked = await http(store, "POST", "", billing=True, validate=True, body=activation(approval))
    assert checked.status_code == 200 and checked.json() == approval
    assert await targets(store) == (0, 0, 0)
    assert (
        await http(store, "DELETE", "sync-authorizations/" + approval["authorization_id"])
    ).status_code == 200
    await paid(store)
    assert (
        await http(store, "POST", "", billing=True, validate=True, body=activation(approval))
    ).status_code == 409
    assert (
        await http(store, "POST", "", billing=True, body=activation(approval))
    ).status_code == 409
    assert await targets(store) == (0, 0, 0)


async def test_paid_activation_is_atomic_idempotent_and_mints_no_general_credential(store):
    approval = await approve(store)
    await paid(store)
    first = await http(store, "POST", "sync-installations", body=activation(approval))
    assert first.status_code == 201, first.text
    result = first.json()
    replay = await http(store, "POST", "", body=activation(approval), billing=True)
    assert replay.status_code == 200 and replay.json() == result
    assert await targets(store) == (1, 1, 1)
    async with store.factory() as db:
        context = await db.get(Context, UUID(result["context_id"]))
        config = await db.scalar(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == context.id)
        )
        assert config is not None and config.embedding_dimensions > 0
        assert context.is_public and not context.is_private
        assert (
            await db.scalar(
                select(ResourceToken.id).where(
                    ResourceToken.resource_pk == UUID(result["resource_pk"])
                )
            )
            is None
        )
        endpoint = await load_serving_endpoint(db, result["public_id"])
        assert endpoint.context_id == context.id and endpoint.public_name == "Approved catalog"
        assert endpoint.public_description == "Approved public text"
    assert first.headers["cache-control"] == "no-store"


async def test_internal_activation_outlives_original_human_login(store):
    approval = await approve(store)
    await paid(store)
    await change(store, OIDCToken, store.token_ids["owner"], revoked_at=store.clock.now)
    await change(store, OIDCGrant, store.grant_ids["owner"], revoked_at=store.clock.now)
    store.clock.now += timedelta(hours=9)
    response = await http(store, "POST", "", billing=True, body=activation(approval))
    assert response.status_code == 200, response.text
    async with store.factory() as db:
        audit = await db.scalar(
            select(AuditLog).where(
                AuditLog.action == "sync.installation.activated",
                AuditLog.resource == "sync:" + response.json()["installation_id"],
            )
        )
        assert audit.user_id == "service:billing-publisher"
        assert "Approved catalog" not in str(audit.user_metadata)


async def test_active_installation_survives_transfer_and_new_owner_can_unlink_without_payment(
    store,
):
    approval = await approve(store)
    await paid(store)
    response = await http(store, "POST", "", billing=True, body=activation(approval))
    result = response.json()
    await change(store, Context, UUID(result["context_id"]), display_name="Retained edited content")
    await transfer(store, "next")
    await paid(store, 2, services=False)
    suffix = "sync-installations/" + result["installation_id"]
    assert (await http(store, "DELETE", suffix)).status_code == 404
    revoked = await http(store, "DELETE", suffix, role="next")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["generation"] == 2 and revoked.json()["state"] == "revoked"
    again = await http(store, "DELETE", suffix, role="next")
    assert again.json() == revoked.json()
    replay = await http(store, "POST", "", billing=True, body=activation(approval))
    assert replay.status_code == 200 and replay.json() == revoked.json()
    assert await targets(store) == (1, 1, 1)
    async with store.factory() as db:
        assert (
            await db.get(Context, UUID(result["context_id"]))
        ).display_name == "Retained edited content"
        assert (await db.get(PublicMcpEndpoint, UUID(result["endpoint_id"]))).revoked_at is None
    fresh = await approve(store, role="next")
    precharge = await http(store, "POST", "", billing=True, validate=True, body=activation(fresh))
    assert precharge.status_code == 409
    assert (await http(store, "POST", "", billing=True, body=activation(fresh))).status_code == 409
    assert await targets(store) == (1, 1, 1)


@pytest.mark.parametrize("missing", ["soft_context", "context", "endpoint", "paused"])
async def test_retained_binding_is_never_recreated_or_resumed_implicitly(store, missing):
    approval = await approve(store)
    await paid(store)
    result = (await http(store, "POST", "", billing=True, body=activation(approval))).json()
    async with store.factory() as db:
        if missing == "soft_context":
            (await db.get(Context, UUID(result["context_id"]))).deleted_at = store.clock.now
        elif missing == "context":
            await db.execute(delete(Context).where(Context.id == UUID(result["context_id"])))
        elif missing == "endpoint":
            (
                await db.get(PublicMcpEndpoint, UUID(result["endpoint_id"]))
            ).revoked_at = store.clock.now
        else:
            (await db.get(SyncInstallation, UUID(result["installation_id"]))).state = "paused"
        await db.commit()
    before = await targets(store)
    replay = await http(store, "POST", "", billing=True, body=activation(approval))
    assert replay.status_code == (200 if missing == "paused" else 409)
    if missing == "paused":
        assert replay.json()["state"] == "paused" and replay.json()["generation"] == 1
    assert await targets(store) == before
    viewed = await http(store, "GET", "sync-installations/" + result["installation_id"])
    assert viewed.status_code == 200 and viewed.json()["resource_pk"] == result["resource_pk"]


async def test_inspection_checks_current_context_visibility_and_explicit_workspace(store):
    approval = await approve(store)
    await paid(store)
    result = (await http(store, "POST", "", billing=True, body=activation(approval))).json()
    path = "sync-installations/" + result["installation_id"]
    assert (await http(store, "GET", path, role="viewer")).status_code == 200
    assert (await http(store, "GET", path, role="outsider")).status_code == 404
    assert (await http(store, "GET", path, workspace=store.other)).status_code == 404
    assert (await http(store, "GET", "sync-installations/" + str(uuid4()))).status_code == 404
    async with store.factory() as db:
        member = await db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == store.wid,
                WorkspaceMember.user_id == store.users["member"],
            )
        )
        member.allowed_context_ids = []
        await db.commit()
    assert (await http(store, "GET", path, role="member")).status_code == 404
    assert (
        await http(store, "DELETE", "sync-authorizations/" + approval["authorization_id"])
    ).status_code == 409
