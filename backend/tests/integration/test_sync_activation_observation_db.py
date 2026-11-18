"""Exact retained activation discovery with live owner/epoch/service fences (#2892)."""

from datetime import UTC, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from models.oidc import OIDCClient
from models.public_mcp import PublicMcpEndpoint
from models.sync_installation import SyncAuthorization, SyncInstallation, SyncService
from tests.integration import sync_installation_fixtures as fixtures
from tests.integration.sync_installation_fixtures import (
    activation,
    approve,
    change,
    http,
    paid,
    targets,
    transfer,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store


async def observe(store, approval, **overrides):
    async with AsyncClient(
        transport=ASGITransport(app=store.app), base_url="https://test"
    ) as client:
        return await client.post(
            f"/internal/workspaces/{store.wid}/sync-installations/observe",
            headers={"Authorization": "Bearer fixture-billing-2802"},
            json={**activation(approval), **overrides},
        )


async def test_pending_observation_does_not_consume_approval_or_provision(store):
    approval = await approve(store)
    response = await observe(store, approval)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract_version"] == "sync-activation-observation-v1"
    assert (
        body["workspace_id"] == str(store.wid)
        and body["application_id"] == approval["application_id"]
    )
    assert body["binding_current"] is True and body["ownership_epoch"] == 0
    assert body["installation"] is None and body["authorization"]["used_at"] is None
    observed = store.clock.now.replace(tzinfo=UTC)
    assert body["observed_at"] == observed.isoformat().replace("+00:00", "Z")
    assert body["valid_until"] == (observed + timedelta(seconds=30)).isoformat().replace(
        "+00:00", "Z"
    )
    assert await targets(store) == (0, 0, 0)
    async with store.factory() as db:
        assert (await db.get(SyncAuthorization, UUID(approval["authorization_id"]))).used_at is None


async def test_lost_reply_discovers_the_same_target_after_original_consent_expires(store):
    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    assert created.status_code == 200, created.text
    before = created.json()
    store.clock.now += timedelta(days=8)
    observed = await observe(store, approval)
    assert observed.status_code == 200, observed.text
    assert observed.json()["binding_current"] is True
    assert observed.json()["installation"] == before
    assert observed.json()["authorization"]["used_at"] is not None
    assert await targets(store) == (1, 1, 1)


# A disabled service or a revoked browser registration is a conflict hold; every
# other change of the original binding answers binding_current=false.
REGISTRATION_HOLDS = {"service_disabled", "browser_revoked"}


@pytest.mark.parametrize(
    "kind",
    [
        "transfer",
        "away_back",
        "service_generation",
        "revoke",
        "pending_expiry",
        "consumed",
        "service_client",
        "operations",
        *sorted(REGISTRATION_HOLDS),
    ],
)
async def test_current_binding_changes_are_holds_without_mutation(store, kind):
    approval = await approve(store)
    if kind in {"transfer", "away_back"}:
        await transfer(store, "next")
        if kind == "away_back":
            await transfer(store, "owner")
    elif kind == "service_generation":
        await change(store, SyncService, store.service_id, generation=2)
    elif kind == "revoke":
        await change(
            store, SyncAuthorization, UUID(approval["authorization_id"]), revoked_at=store.clock.now
        )
    elif kind == "consumed":
        await change(
            store, SyncAuthorization, UUID(approval["authorization_id"]), used_at=store.clock.now
        )
    elif kind == "service_client":
        await change(
            store, SyncService, store.service_id, service_client_id=store.service_id + "-other"
        )
    elif kind == "operations":
        await change(
            store,
            SyncAuthorization,
            UUID(approval["authorization_id"]),
            operations=["schema:read", "records:write"],
        )
    elif kind == "service_disabled":
        await change(store, SyncService, store.service_id, enabled=False)
    elif kind == "browser_revoked":
        await change(store, OIDCClient, store.client_id, revoked_at=store.clock.now)
    else:
        store.clock.now += timedelta(days=8)
    response = await observe(store, approval)
    if kind in REGISTRATION_HOLDS:
        assert response.status_code == 409, response.text
    else:
        assert response.status_code == 200, response.text
        assert response.json()["binding_current"] is False
        assert response.json()["installation"] is None
    assert await targets(store) == (0, 0, 0)
    async with store.factory() as db:
        assert (await db.get(SyncAuthorization, UUID(approval["authorization_id"]))).used_at == (
            store.clock.now if kind == "consumed" else None
        )


async def test_consumed_original_observation_detects_transfer_away_and_back(store):
    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    assert created.status_code == 200
    await transfer(store, "next")
    await transfer(store, "owner")
    response = await observe(store, approval)
    assert response.status_code == 200, response.text
    assert response.json()["ownership_epoch"] == 2
    assert response.json()["binding_current"] is False
    assert response.json()["installation"] == created.json()
    assert await targets(store) == (1, 1, 1)


@pytest.mark.parametrize("kind,status", [("application", 409), ("authorization", 404)])
async def test_other_references_are_never_adopted(store, kind, status):
    approval = await approve(store)
    response = await observe(
        store,
        approval,
        **{"application_id": "other"}
        if kind == "application"
        else {"authorization_id": str(uuid4())},
    )
    assert response.status_code == status, response.text
    assert await targets(store) == (0, 0, 0)


async def test_retained_installation_of_another_application_is_a_conflict(store):
    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    assert created.status_code == 200, created.text
    iid = UUID(created.json()["installation_id"])
    await change(store, SyncInstallation, iid, application_id="application-2")
    response = await observe(store, approval)
    assert response.status_code == 409, response.text
    async with store.factory() as db:
        assert (await db.get(SyncInstallation, iid)).application_id == "application-2"


async def test_rebound_retained_row_is_observed_but_never_current(store):
    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    assert created.status_code == 200
    async with store.factory() as db:
        row = await db.scalar(
            select(SyncInstallation).where(SyncInstallation.workspace_id == store.wid)
        )
        prior = await db.get(SyncAuthorization, UUID(approval["authorization_id"]))
        # Another retained consent must not become the original operation's result.
        newer = SyncAuthorization(
            **{
                key: getattr(prior, key)
                for key in (
                    "workspace_id",
                    "application_id",
                    "service_id",
                    "service_generation",
                    "browser_client_id",
                    "service_client_id",
                    "operations",
                    "authorized_by",
                    "ownership_epoch",
                    "request_hash",
                    "public_name",
                    "created_at",
                    "expires_at",
                )
            },
            id=uuid4(),
            request_id=uuid4(),
        )
        db.add(newer)
        await db.flush()
        row.authorization_id = newer.id
        await db.commit()
    response = await observe(store, approval)
    assert response.status_code == 200, response.text
    assert response.json()["binding_current"] is False
    assert response.json()["authorization"]["authorization_id"] == approval["authorization_id"]
    assert response.json()["installation"]["installation_id"] == created.json()["installation_id"]
    assert response.text.count(str(newer.id)) == 0


async def reconnected(store, monkeypatch):
    """Activate with consent A, unlink, then reconnect the same row with fresh consent B."""
    from api.routes import sync_management as management_routes
    from services import sync_feed_authority, sync_management, sync_resume

    for name in ("sync_runs_enabled", "sync_managed_enabled"):
        monkeypatch.setattr(sync_management.get_settings(), name, True)
    for module in (sync_feed_authority, sync_management, sync_resume):
        monkeypatch.setattr(module, "utcnow", lambda: store.clock.now)
    store.app.include_router(management_routes.router, prefix="/api/v1")
    original = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(original))
    assert created.status_code == 200, created.text
    iid = UUID(created.json()["installation_id"])
    async with store.factory() as db:
        await sync_management.enroll(db, await db.get(SyncInstallation, iid))
        await db.commit()
    unlinked = await http(store, "DELETE", f"sync-installations/{iid}")
    assert unlinked.status_code == 200, unlinked.text
    async with store.factory() as db:
        assert await sync_management.finish_stopping(db, await db.get(SyncInstallation, iid))
        await db.commit()
    fresh = await approve(store)
    command = {"operation_id": str(uuid4()), "authorization_id": fresh["authorization_id"]}
    result = await http(
        store, "POST", f"sync-installations/{iid}/management/reconnect", body=command
    )
    assert result.status_code == 200, result.text
    assert result.json()["mode"] == "manual"
    return original, fresh, created.json()


async def test_reconnected_original_observes_its_target_as_no_longer_current(store, monkeypatch):
    original, fresh, created = await reconnected(store, monkeypatch)
    response = await observe(store, original)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["binding_current"] is False
    assert body["authorization"]["authorization_id"] == original["authorization_id"]
    assert body["installation"]["installation_id"] == created["installation_id"]
    assert body["installation"]["application_id"] == created["application_id"]
    assert body["installation"]["service_client_id"] == created["service_client_id"]
    assert body["installation"]["state"] == "active"
    assert body["management"]["mode"] == "manual"
    assert body["management"]["generation"] == body["installation"]["generation"]
    observed = store.clock.now.replace(tzinfo=UTC)
    assert body["valid_until"] == (observed + timedelta(seconds=30)).isoformat().replace(
        "+00:00", "Z"
    )
    # Nothing in the observation names or describes the newer approval.
    assert response.text.count(fresh["authorization_id"]) == 0
    assert await targets(store) == (1, 1, 1)
    async with store.factory() as db:
        row = await db.get(SyncInstallation, UUID(created["installation_id"]))
        assert row.authorization_id == UUID(fresh["authorization_id"])


@pytest.mark.parametrize("state", ["paused", "revoked"])
async def test_read_preserves_paused_or_revoked_target_and_generation(store, state):
    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    iid = UUID(created.json()["installation_id"])
    await change(
        store,
        SyncInstallation,
        iid,
        state=state,
        generation=2,
        revoked_at=store.clock.now if state == "revoked" else None,
    )
    response = await observe(store, approval)
    assert response.status_code == 200, response.text
    assert response.json()["installation"]["state"] == state
    assert response.json()["installation"]["generation"] == 2
    async with store.factory() as db:
        assert (await db.get(SyncInstallation, iid)).state == state


async def test_final_deadline_cannot_return_an_expired_observation(store, monkeypatch):
    from services import sync_installations as service

    approval = await approve(store)
    original_view = service.authorization_view

    def slow_view(row):
        value = original_view(row)
        store.clock.now += timedelta(seconds=31)
        return value

    monkeypatch.setattr(service, "authorization_view", slow_view)
    response = await observe(store, approval)
    assert response.status_code == 409 and await targets(store) == (0, 0, 0)


async def facts(store, approval, iid=None):
    """Every column an observation could plausibly touch."""
    async with store.factory() as db:
        consent = await db.get(SyncAuthorization, UUID(approval["authorization_id"]))
        row = await db.get(SyncInstallation, iid) if iid is not None else None
        return (
            await targets(store),
            (consent.used_at, consent.revoked_at, consent.expires_at),
            (row.authorization_id, row.state, row.generation) if row is not None else None,
        )


def slow_observation(store, monkeypatch, delay):
    """Move the clock while the observation is being assembled, before its commit."""
    from services import sync_installations as service

    original_view = service.authorization_view

    def slow_view(row):
        value = original_view(row)
        store.clock.now += delay
        return value

    monkeypatch.setattr(service, "authorization_view", slow_view)


async def test_retained_target_observation_cannot_outlive_its_deadline(store, monkeypatch):
    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    assert created.status_code == 200, created.text
    iid = UUID(created.json()["installation_id"])
    before = await facts(store, approval, iid)
    slow_observation(store, monkeypatch, timedelta(seconds=31))
    response = await observe(store, approval)
    assert response.status_code == 409, response.text
    assert await facts(store, approval, iid) == before


async def test_pending_expiry_deadline_is_enforced_before_commit(store, monkeypatch):
    approval = await approve(store)
    end = store.clock.now + timedelta(seconds=7)
    await change(store, SyncAuthorization, UUID(approval["authorization_id"]), expires_at=end)
    before = await facts(store, approval)
    slow_observation(store, monkeypatch, timedelta(seconds=8))
    response = await observe(store, approval)
    assert response.status_code == 409, response.text
    assert await facts(store, approval) == before


async def test_mode_row_generation_drift_is_a_conflict(store):
    from services import sync_management

    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    iid = UUID(created.json()["installation_id"])
    async with store.factory() as db:
        row = await db.get(SyncInstallation, iid)
        await sync_management.start_stopping(db, row, "manual_requested")
        await db.commit()
        generation = row.generation
    await change(store, SyncInstallation, iid, generation=generation + 1)
    response = await observe(store, approval)
    assert response.status_code == 409, response.text


async def test_stop_and_manual_holds_remain_distinct_without_reenrollment(store):
    from models.sync_management import SyncManagedTarget
    from services import sync_management

    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    iid = UUID(created.json()["installation_id"])
    initial = await observe(store, approval)
    assert initial.json()["management"]["mode"] == "feed"
    async with store.factory() as db:
        assert await db.get(SyncManagedTarget, iid) is None
        row = await db.get(SyncInstallation, iid)
        await sync_management.start_stopping(db, row, "manual_requested")
        await db.commit()
    stopping = await observe(store, approval)
    assert stopping.json()["installation"]["state"] == "active"
    assert stopping.json()["management"]["mode"] == "stopping"
    async with store.factory() as db:
        row = await db.get(SyncInstallation, iid)
        assert await sync_management.finish_stopping(db, row)
        await db.commit()
    manual = await observe(store, approval)
    assert manual.json()["installation"]["state"] == "active"
    assert manual.json()["management"]["mode"] == "manual"
    assert manual.json()["management"]["generation"] == manual.json()["installation"]["generation"]


async def test_authorization_from_another_workspace_is_private(store):
    approval = await http(
        store, "POST", "sync-authorizations", body=fixtures.approval_body(), workspace=store.other
    )
    assert approval.status_code == 201
    response = await observe(store, approval.json())
    assert response.status_code == 404
    assert await targets(store) == (0, 0, 0)


async def test_missing_physical_target_is_observed_without_replacement(store):
    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    original = created.json()
    async with store.factory() as db:
        await db.execute(
            delete(PublicMcpEndpoint).where(PublicMcpEndpoint.id == UUID(original["endpoint_id"]))
        )
        await db.commit()
    response = await observe(store, approval)
    assert response.status_code == 200, response.text
    result = response.json()["installation"]
    assert result["target_available"] is False
    assert result["installation_id"] == original["installation_id"]
    assert result["endpoint_id"] == original["endpoint_id"]
    assert await targets(store) == (1, 1, 0)


async def test_pending_expiry_shortens_the_observation(store):
    approval = await approve(store)
    end = store.clock.now + timedelta(seconds=7)
    await change(store, SyncAuthorization, UUID(approval["authorization_id"]), expires_at=end)
    response = await observe(store, approval)
    assert response.status_code == 200, response.text
    assert response.json()["binding_current"] is True
    assert response.json()["valid_until"] == end.replace(tzinfo=UTC).isoformat().replace(
        "+00:00", "Z"
    )


async def test_mode_row_moved_off_retained_context_is_a_conflict(store):
    from models.sync_management import SyncManagedTarget
    from services import sync_management

    approval = await approve(store)
    await paid(store)
    created = await http(store, "POST", "", billing=True, body=activation(approval))
    iid = UUID(created.json()["installation_id"])
    async with store.factory() as db:
        row = await db.get(SyncInstallation, iid)
        await sync_management.start_stopping(db, row, "manual_requested")
        await db.commit()
    await change(store, SyncManagedTarget, iid, context_id=uuid4())
    response = await observe(store, approval)
    assert response.status_code == 409
