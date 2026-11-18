"""Read-only continuity/reconnect facts keep owner transfer separate from new authority."""

from datetime import UTC, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from config.settings import get_settings
from models.auth import Context, User
from models.oidc import OIDCClient
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from models.sync_installation import SyncAuthorization, SyncInstallation, SyncService
from models.sync_installation_schemas import SyncActivation, SyncReconnectValidation
from models.sync_management import SyncManagedTarget
from services import sync_continuity, sync_installations, sync_management
from services.workspace_locks import lock_workspace_for_update
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
from utils.exceptions import ConflictError, NotFoundException

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store


@pytest.fixture(autouse=True)
def observation_clock(store, monkeypatch):
    monkeypatch.setattr(sync_continuity, "utcnow", lambda: store.clock.now)


async def request(store, approval, *, reconnect=False, installation=None, workspace=None, **extra):
    suffix = (
        "sync-authorizations/validate-reconnect"
        if reconnect
        else "sync-installations/observe-established"
    )
    body = {**activation(approval), **extra}
    if installation is not None:
        body["installation_id"] = str(installation)
    async with AsyncClient(
        transport=ASGITransport(app=store.app), base_url="https://test"
    ) as client:
        return await client.post(
            f"/internal/workspaces/{workspace or store.wid}/{suffix}",
            headers={"Authorization": "Bearer fixture-billing-2802"},
            json=body,
        )


async def established(store):
    approval = await approve(store)
    await paid(store)
    response = await http(store, "POST", "", billing=True, body=activation(approval))
    assert response.status_code == 200, response.text
    return approval, response.json()


@pytest.mark.parametrize(
    "kind", ["transfer", "away_back", "original_owner_deleted", "expired_original"]
)
async def test_established_binding_survives_owner_change_without_rewriting_original(store, kind):
    approval, created = await established(store)
    await transfer(store, "next")
    if kind == "away_back":
        await transfer(store, "owner")
    if kind == "original_owner_deleted":
        async with store.factory() as db:
            await db.delete(
                await db.scalar(select(User).where(User.user_id == store.users["owner"]))
            )
            await db.commit()
    if kind == "expired_original":
        store.clock.now += timedelta(days=8)
    response = await request(store, approval)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract_version"] == "sync-established-target-observation-v1"
    assert body["continuity_current"] is True
    assert body["ownership_epoch"] == (2 if kind == "away_back" else 1)
    assert body["authorization"]["ownership_epoch"] == 0
    assert body["installation"] == created
    assert body["authorization"]["used_at"] is not None
    assert response.headers["cache-control"] == "no-store"
    assert await targets(store) == (1, 1, 1)
    async with store.factory() as db:
        row = await db.get(SyncInstallation, UUID(created["installation_id"]))
        assert row.authorization_id == UUID(approval["authorization_id"])
        assert row.generation == created["generation"] and row.state == "active"
    async with AsyncClient(
        transport=ASGITransport(app=store.app), base_url="https://test"
    ) as client:
        old = await client.post(
            f"/internal/workspaces/{store.wid}/sync-installations/observe",
            headers={"Authorization": "Bearer fixture-billing-2802"},
            json=activation(approval),
        )
    assert old.status_code == 200 and old.json()["binding_current"] is False
    assert set(old.json()) == {
        "contract_version",
        "workspace_id",
        "application_id",
        "authorization",
        "ownership_epoch",
        "binding_current",
        "installation",
        "management",
        "observed_at",
        "valid_until",
    }
    assert "continuity_current" not in old.json()


@pytest.mark.parametrize("consumed", [False, True])
async def test_unestablished_approval_never_becomes_continuous_or_creates_target(store, consumed):
    approval = await approve(store)
    if consumed:
        await change(
            store, SyncAuthorization, UUID(approval["authorization_id"]), used_at=store.clock.now
        )
    response = await request(store, approval)
    assert response.status_code == 200, response.text
    assert response.json()["continuity_current"] is False
    assert response.json()["installation"] is None and response.json()["management"] is None
    assert await targets(store) == (0, 0, 0)


@pytest.mark.parametrize(
    "kind",
    [
        "approval_revoked",
        "service_generation",
        "service_client",
        "browser_client",
        "approval_operations",
        "installation_client",
        "installation_operations",
        "unconsumed",
        "rebound",
        "service_disabled",
        "browser_revoked",
    ],
)
async def test_continuity_does_not_ignore_other_binding_failures(store, kind):
    approval, created = await established(store)
    aid, iid = UUID(approval["authorization_id"]), UUID(created["installation_id"])
    if kind == "approval_revoked":
        await change(store, SyncAuthorization, aid, revoked_at=store.clock.now)
    elif kind == "service_generation":
        await change(store, SyncService, store.service_id, generation=2)
    elif kind == "service_client":
        await change(store, SyncService, store.service_id, service_client_id="changed-worker")
    elif kind == "browser_client":
        await change(store, SyncService, store.service_id, browser_client_id=store.foreign_client)
    elif kind == "approval_operations":
        await change(store, SyncAuthorization, aid, operations=["schema:read"])
    elif kind == "installation_client":
        await change(store, SyncInstallation, iid, service_client_id="changed-worker")
    elif kind == "installation_operations":
        await change(store, SyncInstallation, iid, operations=["schema:read"])
    elif kind == "unconsumed":
        await change(store, SyncAuthorization, aid, used_at=None)
    elif kind == "rebound":
        fresh = await approve(store)
        await change(store, SyncInstallation, iid, authorization_id=UUID(fresh["authorization_id"]))
    elif kind == "service_disabled":
        await change(store, SyncService, store.service_id, enabled=False)
    else:
        await change(store, OIDCClient, store.client_id, revoked_at=store.clock.now)
    response = await request(store, approval)
    if kind in {"service_disabled", "browser_revoked"}:
        assert response.status_code == 409, response.text
    else:
        assert response.status_code == 200, response.text
        assert response.json()["continuity_current"] is False
    if kind == "rebound":
        # The retained row is reported, under the requested consent, never the rebound one.
        assert response.json()["installation"]["installation_id"] == created["installation_id"]
        assert response.json()["authorization"]["authorization_id"] == approval["authorization_id"]
        assert fresh["authorization_id"] not in response.text
    assert await targets(store) == (1, 1, 1)


@pytest.mark.parametrize(
    "kind",
    [
        "context_private",
        "context_deleted",
        "context_moved",
        "resource_moved",
        "resource_missing",
        "endpoint_revoked",
    ],
)
async def test_unavailable_physical_original_is_not_continuous(store, kind):
    approval, created = await established(store)
    if kind.startswith("context"):
        values = (
            {"is_private": True}
            if kind.endswith("private")
            else (
                {"deleted_at": store.clock.now}
                if kind.endswith("deleted")
                else {"workspace_id": store.other}
            )
        )
        await change(store, Context, UUID(created["context_id"]), **values)
    elif kind == "resource_moved":
        await change(store, Resource, UUID(created["resource_pk"]), workspace_id=store.other)
    elif kind == "resource_missing":
        async with store.factory() as db:
            await db.delete(await db.get(Resource, UUID(created["resource_pk"])))
            await db.commit()
    else:
        await change(
            store, PublicMcpEndpoint, UUID(created["endpoint_id"]), revoked_at=store.clock.now
        )
    response = await request(store, approval)
    assert response.status_code == 200, response.text
    assert response.json()["continuity_current"] is False
    assert response.json()["installation"]["target_available"] is False


@pytest.mark.parametrize("mode", ["manual", "stopping"])
async def test_continuity_keeps_management_holds_explicit_and_never_resumes(
    store, monkeypatch, mode
):
    approval, created = await established(store)
    monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", True)
    monkeypatch.setattr(sync_management, "utcnow", lambda: store.clock.now)
    iid = UUID(created["installation_id"])
    async with store.factory() as db:
        row = await db.get(SyncInstallation, iid)
        await sync_management.enroll(db, row)
        await sync_management.start_stopping(db, row, "manual_request")
        if mode == "manual":
            await sync_management.finish_stopping(db, row)
        await db.commit()
    response = await request(store, approval)
    assert response.status_code == 200, response.text
    assert response.json()["management"]["mode"] == mode
    async with store.factory() as db:
        assert (await db.get(SyncManagedTarget, iid)).mode == mode


@pytest.mark.parametrize("state", ["paused", "revoked"])
async def test_established_observation_keeps_installation_holds_explicit(store, state):
    approval, created = await established(store)
    iid = UUID(created["installation_id"])
    await change(
        store,
        SyncInstallation,
        iid,
        state=state,
        revoked_at=store.clock.now if state == "revoked" else None,
    )
    response = await request(store, approval)
    assert response.status_code == 200, response.text
    assert response.json()["installation"]["state"] == state
    async with store.factory() as db:
        assert (await db.get(SyncInstallation, iid)).state == state


async def retained_manual(store, monkeypatch):
    approval, created = await established(store)
    # Every switch the reconnect itself checks is on (#2917).
    monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", True)
    monkeypatch.setattr(sync_management.get_settings(), "sync_runs_enabled", True)
    monkeypatch.setattr(sync_management, "utcnow", lambda: store.clock.now)
    iid = UUID(created["installation_id"])
    async with store.factory() as db:
        await sync_management.enroll(db, await db.get(SyncInstallation, iid))
        await db.commit()
    unlinked = await http(store, "DELETE", f"sync-installations/{iid}")
    assert unlinked.status_code == 200, unlinked.text
    async with store.factory() as db:
        await sync_management.finish_stopping(db, await db.get(SyncInstallation, iid))
        await db.commit()
    fresh = await approve(store)
    # Validation precedes a future charge: no additional paid service right is present.
    await paid(store, revision=2, services=False)
    return approval, fresh, created


async def test_current_new_owner_must_obtain_fresh_pending_reconnect_consent(store, monkeypatch):
    _, old_pending, created = await retained_manual(store, monkeypatch)
    await transfer(store, "next")
    fresh = await approve(store, role="next")
    response = await request(store, fresh, reconnect=True, installation=created["installation_id"])
    assert response.status_code == 200, response.text
    assert response.json()["ownership_epoch"] == 1
    assert response.json()["authorization"]["ownership_epoch"] == 1
    assert response.json()["authorization"]["used_at"] is None
    assert old_pending["authorization_id"] not in response.text


async def test_reconnect_validation_reads_current_pending_owner_and_exact_retained_target(
    store, monkeypatch
):
    old, fresh, created = await retained_manual(store, monkeypatch)
    iid = UUID(created["installation_id"])
    await change(
        store,
        SyncAuthorization,
        UUID(fresh["authorization_id"]),
        expires_at=store.clock.now + timedelta(seconds=7),
    )
    response = await request(store, fresh, reconnect=True, installation=iid)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract_version"] == "sync-reconnect-validation-v1"
    assert body["authorization"]["authorization_id"] == fresh["authorization_id"]
    assert body["authorization"]["used_at"] is None and body["ownership_epoch"] == 0
    assert body["installation"]["installation_id"] == str(iid)
    assert (
        body["installation"]["state"] == "revoked"
        and body["installation"]["target_available"] is True
    )
    assert body["management"]["mode"] == "manual"
    assert body["valid_until"] == (store.clock.now + timedelta(seconds=7)).replace(
        tzinfo=UTC
    ).isoformat().replace("+00:00", "Z")
    assert response.headers["cache-control"] == "no-store"
    assert await targets(store) == (1, 1, 1)
    async with store.factory() as db:
        assert (await db.get(SyncAuthorization, UUID(fresh["authorization_id"]))).used_at is None
        row = await db.get(SyncInstallation, iid)
        assert row.authorization_id == UUID(old["authorization_id"]) and row.state == "revoked"
        assert (await db.get(SyncManagedTarget, iid)).mode == "manual"
    initial = await http(store, "POST", "", billing=True, validate=True, body=activation(fresh))
    assert initial.status_code == 409  # initial provisioning contract remains unchanged


@pytest.mark.parametrize(
    "kind",
    [
        "stale_owner",
        "revoked",
        "used",
        "expired",
        "wrong_selector",
        "wrong_application",
        "wrong_service",
        "not_revoked",
        "not_manual",
        "physical_missing",
    ],
)
async def test_reconnect_validation_refuses_unready_authority_or_retained_target(
    store, monkeypatch, kind
):
    _, fresh, created = await retained_manual(store, monkeypatch)
    aid, iid = UUID(fresh["authorization_id"]), UUID(created["installation_id"])
    selected = iid
    if kind == "stale_owner":
        await transfer(store, "next")
    elif kind in {"revoked", "used"}:
        key = {"revoked": "revoked_at", "used": "used_at"}[kind]
        await change(store, SyncAuthorization, aid, **{key: store.clock.now})
    elif kind == "expired":
        store.clock.now += timedelta(days=8)
    elif kind == "wrong_selector":
        selected = uuid4()
    elif kind == "wrong_application":
        fresh = {**fresh, "application_id": "foreign-application"}
    elif kind == "wrong_service":
        await change(store, SyncInstallation, iid, service_client_id="foreign-worker")
    elif kind == "not_revoked":
        await change(store, SyncInstallation, iid, state="active", revoked_at=None)
    elif kind == "not_manual":
        await change(store, SyncManagedTarget, iid, mode="stopping")
    else:
        await change(store, Context, UUID(created["context_id"]), deleted_at=store.clock.now)
    response = await request(store, fresh, reconnect=True, installation=selected)
    assert response.status_code in {404, 409}, response.text
    async with store.factory() as db:
        assert (await db.get(SyncAuthorization, aid)).used_at == (
            store.clock.now if kind == "used" else None
        )


@pytest.mark.parametrize("reconnect", [False, True])
async def test_observations_reject_foreign_workspace_and_expiry_at_commit(
    store, monkeypatch, reconnect
):
    if reconnect:
        _, approval, created = await retained_manual(store, monkeypatch)
    else:
        approval, created = await established(store)
    iid = UUID(created["installation_id"])
    foreign = await request(
        store,
        approval,
        reconnect=reconnect,
        installation=iid if reconnect else None,
        workspace=store.other,
    )
    assert foreign.status_code == 404, foreign.text
    original = sync_installations.installation_view

    async def expired(db, row):
        result = await original(db, row)
        store.clock.now += timedelta(seconds=31)
        return result

    monkeypatch.setattr(sync_installations, "installation_view", expired)
    response = await request(
        store, approval, reconnect=reconnect, installation=iid if reconnect else None
    )
    assert response.status_code == 409, response.text


async def test_established_observation_serializes_with_workspace_transfer_or_reconnect(store):
    approval, created = await established(store)
    async with store.factory() as db:
        await lock_workspace_for_update(db, store.wid)
        response = await request(store, approval)
        assert response.status_code == 409, response.text
        await db.rollback()
    response = await request(store, approval)
    assert response.status_code == 200 and response.json()["continuity_current"] is True
    assert response.json()["installation"] == created


async def test_owner_controls_wait_out_a_short_workspace_holder(store):
    """A memory create holds the workspace row from its quota check to commit (#2914)."""
    import asyncio

    from services.workspace_locks import NOWAIT_LOCK_BACKOFF_SECONDS

    approval, created = await established(store)
    async with store.factory() as db:
        await lock_workspace_for_update(db, store.wid)

        async def let_go():
            await asyncio.sleep(NOWAIT_LOCK_BACKOFF_SECONDS * 4)
            await db.rollback()

        released = asyncio.create_task(let_go())
        response = await request(store, approval)
        await released
    assert response.status_code == 200, response.text
    assert response.json()["installation"] == created


async def direct(store, approval, *, installation=None):
    """Call the service itself, so a test can pin the refusal's own detail."""
    async with store.factory() as db:
        if installation is None:
            body = SyncActivation.model_validate(activation(approval))
            return await sync_continuity.observe_established(db, store.wid, body)
        body = SyncReconnectValidation.model_validate(
            {**activation(approval), "installation_id": str(installation)}
        )
        return await sync_continuity.validate_reconnect(db, store.wid, body)


@pytest.mark.parametrize(
    ("switches", "detail"),
    [
        ({"sync_managed_enabled": False}, "Synchronization management not found"),
        ({"sync_runs_enabled": False}, "Synchronization runs not found"),
        # The reconnect checks management first, so validation does too.
        (
            {"sync_managed_enabled": False, "sync_runs_enabled": False},
            "Synchronization management not found",
        ),
    ],
)
async def test_reconnect_validation_is_not_found_while_a_reconnect_switch_is_off(
    store, monkeypatch, switches, detail
):
    _, fresh, created = await retained_manual(store, monkeypatch)
    for name, value in switches.items():
        monkeypatch.setattr(get_settings(), name, value)
    iid = created["installation_id"]
    response = await request(store, fresh, reconnect=True, installation=iid)
    assert response.status_code == 404, response.text
    assert response.json() == {"error": "not_found"}
    async with store.factory() as db:
        body = SyncReconnectValidation.model_validate({**activation(fresh), "installation_id": iid})
        with pytest.raises(NotFoundException, match=f"^{detail}$"):
            await sync_continuity.validate_reconnect(db, store.wid, body)
        assert not db.in_transaction()  # refused before any state was read


@pytest_asyncio.fixture(loop_scope="session")
async def second_service(store):
    """A second registered service, on the fixture's other browser client."""
    service_id = store.service_id + "-second"
    async with store.factory() as db:
        db.add(
            SyncService(
                service_id=service_id,
                browser_client_id=store.foreign_client,
                service_client_id=service_id + "-worker",
            )
        )
        await db.commit()
    yield service_id
    async with store.factory() as db:
        await db.execute(
            delete(SyncAuthorization).where(SyncAuthorization.service_id == service_id)
        )
        await db.execute(delete(SyncService).where(SyncService.service_id == service_id))
        await db.commit()


@pytest.mark.parametrize("differs", ["application", "service"])
async def test_reconnect_validation_refuses_consent_for_another_application_or_service(
    store, monkeypatch, second_service, differs
):
    _, fresh, created = await retained_manual(store, monkeypatch)
    if differs == "application":
        consent = await approve(store, body=approval_body(application_id="application-2"))
        assert consent["application_id"] == "application-2"
    else:
        consent = fresh
        await change(
            store,
            SyncAuthorization,
            UUID(fresh["authorization_id"]),
            service_id=second_service,
            browser_client_id=store.foreign_client,
            service_client_id=second_service + "-worker",
        )
    # The body names the consent's own application, so the request-body check passes.
    iid = created["installation_id"]
    response = await request(store, consent, reconnect=True, installation=iid)
    assert response.status_code == 409, response.text
    with pytest.raises(
        ConflictError, match="^Owner consent does not match the retained installation$"
    ):
        await direct(store, consent, installation=iid)


async def drifted_management(store, monkeypatch, reconnect, drift):
    if reconnect:
        _, consent, created = await retained_manual(store, monkeypatch)
    else:
        consent, created = await established(store)
        async with store.factory() as db:
            row = await db.get(SyncInstallation, UUID(created["installation_id"]))
            await sync_management.start_stopping(db, row, "manual_requested")
            await db.commit()
    iid = UUID(created["installation_id"])
    if drift == "generation":
        async with store.factory() as db:
            generation = (await db.get(SyncInstallation, iid)).generation
        await change(store, SyncInstallation, iid, generation=generation + 1)
    else:
        await change(store, SyncManagedTarget, iid, context_id=uuid4())
    return consent, iid


@pytest.mark.parametrize("drift", ["generation", "context"])
@pytest.mark.parametrize(
    "reconnect", [False, True], ids=["observe_established", "validate_reconnect"]
)
async def test_mode_row_drift_is_a_conflict_for_established_and_reconnect_reads(
    store, monkeypatch, reconnect, drift
):
    consent, iid = await drifted_management(store, monkeypatch, reconnect, drift)
    installation = iid if reconnect else None
    response = await request(store, consent, reconnect=reconnect, installation=installation)
    assert response.status_code == 409, response.text
    with pytest.raises(ConflictError, match="^Retained target management changed; retry$"):
        await direct(store, consent, installation=installation)
    async with store.factory() as db:
        assert (await db.get(SyncManagedTarget, iid)) is not None  # never re-enrolled


async def test_established_observation_refuses_a_foreign_application(store):
    _, created = await established(store)
    consent = await approve(store, body=approval_body(application_id="application-2"))
    response = await request(store, consent)
    assert response.status_code == 409, response.text
    with pytest.raises(
        ConflictError, match="^Workspace has a different retained synchronization approval$"
    ):
        await direct(store, consent)
    async with store.factory() as db:
        assert (
            await db.get(SyncInstallation, UUID(created["installation_id"]))
        ).application_id == "application-1"
