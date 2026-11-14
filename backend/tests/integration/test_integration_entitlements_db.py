"""Live HTTP, ordering, expiry and committing-writer tests with PostgreSQL (#2798)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes import internal_integrations
from api.routes.internal_billing import BillingPlanPush, set_workspace_plan_from_billing
from config.settings import get_settings
from db.base import get_db
from models.auth import Context, User, Workspace
from models.integration_entitlement import WorkspaceIntegrationEvent, WorkspaceIntegrationState
from models.integration_entitlement_schemas import IntegrationEntitlementPush
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from services import integration_entitlements as service
from utils.exceptions import AuthorizationError, MemoryCloudException

pytestmark = pytest.mark.asyncio(loop_scope="session")
NOW = datetime(2026, 11, 14, 0, 0)
SERVICE = "catalog-sync"


def snapshot(revision=1, *, services=None, max_end=None, service_end=None):
    start = (NOW - timedelta(days=1)).replace(tzinfo=UTC).isoformat()
    end = (NOW + timedelta(days=7)).replace(tzinfo=UTC).isoformat()
    window = {"valid_from": start, "valid_until": max_end or end, "evidence_ref": "paid-base-term"}
    return {
        "revision": revision,
        "event_id": f"delivery-{revision}",
        "source_ref": "workspace-snapshot",
        "max_access": window,
        "services": services
        if services is not None
        else [
            {
                "service_id": SERVICE,
                "valid_from": start,
                "valid_until": service_end or end,
                "evidence_ref": "paid-service-term",
            }
        ],
    }


@pytest_asyncio.fixture(loop_scope="session")
async def store(async_engine, monkeypatch):
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    wid, other, cid, rid, endpoint_id = (uuid4() for _ in range(5))
    uid = "paid-integration-" + uuid4().hex
    async with factory() as db:
        db.add(User(user_id=uid, email=uid + "@example.test"))
        await db.flush()
        db.add_all(
            [
                Workspace(
                    id=wid, name="Entitlement fixture", owner_user_id=uid, plan_name="promax"
                ),
                Workspace(id=other, name="Isolated fixture", owner_user_id=uid, plan_name="promax"),
            ]
        )
        await db.flush()
        db.add(Resource(id=rid, workspace_id=wid, resource_id=rid.hex, created_by=uid))
        db.add(
            Context(
                id=cid,
                workspace_id=wid,
                name="catalog",
                created_by=uid,
                resource_id=rid.hex,
                is_private=False,
                is_public=True,
                display_name="Original",
            )
        )
        await db.flush()
        db.add(
            PublicMcpEndpoint(
                id=endpoint_id,
                workspace_id=wid,
                context_id=cid,
                resource_pk=rid,
                resource_id=rid.hex,
                public_name="Catalog",
                created_by=uid,
            )
        )
        await db.commit()
    monkeypatch.setattr(get_settings(), "billing_service_token", "test-billing-service")
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    app = FastAPI()
    app.include_router(internal_integrations.router)

    @app.exception_handler(MemoryCloudException)
    async def canonical_error(_request, exc):
        return JSONResponse({"error": exc.error_code}, status_code=exc.status_code)

    async def sessions():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = sessions
    try:
        yield SimpleNamespace(
            factory=factory,
            app=app,
            wid=wid,
            other=other,
            cid=cid,
            endpoint_id=endpoint_id,
            uid=uid,
        )
    finally:
        async with factory() as db:
            await db.execute(delete(Workspace).where(Workspace.id.in_([wid, other])))
            await db.execute(delete(User).where(User.user_id == uid))
            await db.commit()


async def request(store, body=None, *, workspace=None):
    path = f"/internal/workspaces/{workspace or store.wid}/integration-entitlements"
    async with AsyncClient(
        transport=ASGITransport(app=store.app), base_url="https://testserver"
    ) as client:
        return await client.request(
            "GET" if body is None else "PUT",
            path,
            json=body,
            headers={"Authorization": "Bearer test-billing-service"},
        )


async def apply(store, body):
    async with store.factory() as db:
        result = await service.apply_integration_entitlements(
            db, store.wid, IntegrationEntitlementPush.model_validate(body)
        )
        await db.commit()
        return result


async def decision(store, workspace=None, service_id=SERVICE):
    async with store.factory() as db:
        return await service.inspect_paid_integration(db, workspace or store.wid, service_id)


async def context_name(store):
    async with store.factory() as db:
        return (await db.get(Context, store.cid)).display_name


async def test_default_denial_and_http_persistence_is_workspace_scoped(store):
    empty = await request(store)
    assert empty.status_code == 200
    assert empty.json()["revision"] == 0 and empty.json()["max_access"] is None
    assert not (await decision(store)).allowed
    first = await request(store, snapshot())
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["source"] == "external_billing"
    assert first.json()["max_access"]["valid_from"].endswith("Z")
    current = await request(store)
    assert current.json()["revision"] == 1
    assert (await decision(store)).allowed
    assert not (await decision(store, store.other)).allowed
    assert not (await decision(store, service_id="other-service")).allowed
    assert (await request(store, workspace=uuid4())).status_code == 404


async def test_duplicate_stale_and_conflicting_receipts_never_restore_old_rights(store):
    original = snapshot()
    assert (await request(store, original)).json()["outcome"] == "applied"
    duplicate = await request(store, original)
    assert duplicate.json()["outcome"] == "duplicate" and not duplicate.json()["applied"]
    revoked = snapshot(3, services=[])
    assert (await request(store, revoked)).status_code == 200
    assert not (await decision(store)).allowed
    replay = await request(store, original)
    assert replay.json()["outcome"] == "duplicate" and replay.json()["revision"] == 3
    stale = await request(store, snapshot(2))
    assert stale.json()["outcome"] == "stale" and stale.json()["revision"] == 3
    changed_event = snapshot(4)
    changed_event["event_id"] = original["event_id"]
    assert (await request(store, changed_event)).status_code == 409
    changed_revision = snapshot(3)
    changed_revision["event_id"] = "different-delivery"
    assert (await request(store, changed_revision)).status_code == 409
    original["source_ref"] = "changed-original"
    assert (await request(store, original)).status_code == 409
    assert not (await decision(store)).allowed
    async with store.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkspaceIntegrationEvent)
                .where(WorkspaceIntegrationEvent.workspace_id == store.wid)
            )
            == 2
        )


async def test_normalized_timezones_and_service_order_are_idempotent(store):
    body = snapshot()
    other = {**body["services"][0], "service_id": "other-service"}
    body["services"].append(other)
    await apply(store, body)
    equivalent = snapshot(services=list(reversed(body["services"])))
    equivalent["max_access"]["valid_from"] = "2026-11-13T09:00:00+09:00"
    replay = await request(store, equivalent)
    assert replay.status_code == 200 and replay.json()["outcome"] == "duplicate"


@pytest.mark.parametrize(
    "mutate", ["duplicate_service", "naive", "unknown", "unbounded", "boolean_revision"]
)
async def test_malformed_snapshot_does_not_mutate_rights(store, mutate):
    body = snapshot()
    if mutate == "duplicate_service":
        body["services"].append(dict(body["services"][0]))
    elif mutate == "naive":
        body["max_access"]["valid_until"] = "2026-11-15T00:00:00"
    elif mutate == "unknown":
        body["paid"] = True
    elif mutate == "unbounded":
        body["services"][0]["valid_until"] = None
    else:
        body["revision"] = True
    assert (await request(store, body)).status_code == 422
    assert (await request(store)).json()["revision"] == 0
    assert not (await decision(store)).allowed


@pytest.mark.parametrize("axis", ["max", "service"])
async def test_half_open_windows_cancel_at_end_and_renew_only_with_new_revision(
    store, monkeypatch, axis
):
    end = NOW + timedelta(seconds=10)
    body = snapshot(**{f"{axis}_end": end.replace(tzinfo=UTC).isoformat()})
    await apply(store, body)
    assert (await decision(store)).allowed  # cancellation reserved; still paid until end
    monkeypatch.setattr(service, "utcnow", lambda: end)
    assert not (await decision(store)).allowed
    monkeypatch.setattr(service, "utcnow", lambda: end + timedelta(microseconds=1))
    assert not (await decision(store)).allowed
    await apply(store, snapshot(2))
    assert (await decision(store)).allowed


async def test_future_start_unconfirmed_max_and_current_plan_are_subtractive(store, monkeypatch):
    body = snapshot()
    body["services"][0]["valid_from"] = (NOW + timedelta(hours=1)).replace(tzinfo=UTC).isoformat()
    await apply(store, body)
    assert not (await decision(store)).allowed
    monkeypatch.setattr(service, "utcnow", lambda: NOW + timedelta(hours=1))
    assert (await decision(store)).allowed
    body = snapshot(2)
    body["max_access"] = None
    await apply(store, body)
    assert not (await decision(store)).allowed
    await apply(store, snapshot(3))
    async with store.factory() as db:
        # Legacy billing still works, but a plan upgrade cannot create a paid
        # integration license or resurrect a removed one.
        await set_workspace_plan_from_billing(
            str(store.wid), BillingPlanPush(plan_name="pro"), None, db
        )
        assert not (await service.inspect_paid_integration(db, store.wid, SERVICE)).allowed
        await set_workspace_plan_from_billing(
            str(store.wid), BillingPlanPush(plan_name="promax"), None, db
        )
        assert (await service.inspect_paid_integration(db, store.wid, SERVICE)).allowed
    await apply(store, snapshot(4, services=[]))
    async with store.factory() as db:
        await set_workspace_plan_from_billing(
            str(store.wid), BillingPlanPush(plan_name="promax"), None, db
        )
        assert not (await service.inspect_paid_integration(db, store.wid, SERVICE)).allowed


async def test_replacing_one_license_keeps_others_content_and_publication(store):
    body = snapshot()
    body["services"].append({**body["services"][0], "service_id": "other-service"})
    await apply(store, body)
    await apply(store, snapshot(2, services=[body["services"][1]]))
    assert not (await decision(store)).allowed
    assert (await decision(store, service_id="other-service")).allowed
    async with store.factory() as db:
        assert (await db.get(Context, store.cid)).display_name == "Original"
        assert (await db.get(PublicMcpEndpoint, store.endpoint_id)).revoked_at is None
        workspace = await db.get(Workspace, store.wid)
        workspace.deleted_at = NOW
        await db.commit()
    assert not (await decision(store)).allowed
    assert (await request(store)).status_code == 404
    assert (await request(store, snapshot(3))).status_code == 404


async def test_stale_identity_map_never_reuses_prior_authority(store):
    await apply(store, snapshot())
    async with store.factory() as reader:
        state = await reader.get(WorkspaceIntegrationState, store.wid)
        assert (await service.inspect_paid_integration(reader, store.wid, SERVICE)).allowed
        await apply(store, snapshot(2, services=[]))
        assert state.revision == 1
        assert not (await service.inspect_paid_integration(reader, store.wid, SERVICE)).allowed
        assert state.revision == 2


async def test_guard_commits_success_and_rolls_back_expiry_or_early_commit(store, monkeypatch):
    end = NOW + timedelta(seconds=1)
    await apply(store, snapshot(service_end=end.replace(tzinfo=UTC).isoformat()))
    async with store.factory() as db:
        async with service.paid_integration_write(db, store.wid, SERVICE):
            (await db.get(Context, store.cid)).display_name = "Committed"
    assert await context_name(store) == "Committed"
    async with store.factory() as db:
        with pytest.raises(AuthorizationError):
            async with service.paid_integration_write(db, store.wid, SERVICE):
                (await db.get(Context, store.cid)).display_name = "Must roll back"
                await db.flush()
                monkeypatch.setattr(service, "utcnow", lambda: end)
    assert await context_name(store) == "Committed"
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    async with store.factory() as db:
        with pytest.raises(RuntimeError, match="owns the commit"):
            async with service.paid_integration_write(db, store.wid, SERVICE):
                (await db.get(Context, store.cid)).display_name = "Early commit"
                await db.commit()
    assert await context_name(store) == "Committed"


async def wait_blocked(factory, pid):
    """Observe a real PG row lock wait, not a timing assumption."""
    deadline = monotonic() + 5
    while monotonic() < deadline:
        async with factory() as observer:
            if await observer.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            ):
                return
        await asyncio.sleep(0.01)
    pytest.fail("Expected PostgreSQL row lock wait was not observed")


@pytest.mark.parametrize("initialized", [False, True])
async def test_reconciliation_read_is_atomic_with_concurrent_publish(
    store, monkeypatch, initialized
):
    if initialized:
        await apply(store, snapshot())
    loaded, finish, pids = asyncio.Event(), asyncio.Event(), asyncio.Queue()
    original_state = service._state

    async with store.factory() as reader:

        async def pause_after_state(db, workspace_id):
            state = await original_state(db, workspace_id)
            if db is reader:
                loaded.set()
                await finish.wait()
            return state

        monkeypatch.setattr(service, "_state", pause_after_state)

        async def publish():
            async with store.factory() as db:
                await pids.put(await db.scalar(text("SELECT pg_backend_pid()")))
                body = snapshot(2, services=[]) if initialized else snapshot()
                await service.apply_integration_entitlements(
                    db, store.wid, IntegrationEntitlementPush.model_validate(body)
                )
                await db.commit()

        read_task = asyncio.create_task(service.read_integration_entitlements(reader, store.wid))
        publish_task = None
        try:
            await asyncio.wait_for(loaded.wait(), 5)
            publish_task = asyncio.create_task(publish())
            await wait_blocked(store.factory, await asyncio.wait_for(pids.get(), 5))
            finish.set()
            view = await asyncio.wait_for(read_task, 5)
            assert view.revision == int(initialized)
            assert bool(view.max_access) == initialized and bool(view.services) == initialized
            await reader.commit()
            await asyncio.wait_for(publish_task, 5)
        finally:
            finish.set()
            for task in [read_task, publish_task]:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


async def test_committing_writer_and_revocation_have_a_serialized_boundary(store):
    await apply(store, snapshot())
    pids = asyncio.Queue()

    async def revoke():
        async with store.factory() as publisher:
            await pids.put(await publisher.scalar(text("SELECT pg_backend_pid()")))
            result = await service.apply_integration_entitlements(
                publisher,
                store.wid,
                IntegrationEntitlementPush.model_validate(snapshot(2, services=[])),
            )
            await publisher.commit()
            return result

    task = None
    try:
        async with store.factory() as db:
            async with service.paid_integration_write(db, store.wid, SERVICE):
                (await db.get(Context, store.cid)).display_name = "Before revocation"
                task = asyncio.create_task(revoke())
                await wait_blocked(store.factory, await pids.get())
        assert (await asyncio.wait_for(task, 5)).applied
        assert await context_name(store) == "Before revocation"
        async with store.factory() as db:
            with pytest.raises(AuthorizationError):
                async with service.paid_integration_write(db, store.wid, SERVICE):
                    pytest.fail("A previously issued credential must not outlive revoked rights")
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_out_of_order_publisher_cannot_restore_older_state(store):
    await apply(store, snapshot())
    pids = asyncio.Queue()

    async def older():
        async with store.factory() as publisher:
            await pids.put(await publisher.scalar(text("SELECT pg_backend_pid()")))
            result = await service.apply_integration_entitlements(
                publisher, store.wid, IntegrationEntitlementPush.model_validate(snapshot(2))
            )
            await publisher.commit()
            return result

    task = None
    try:
        async with store.factory() as newer:
            await service.apply_integration_entitlements(
                newer,
                store.wid,
                IntegrationEntitlementPush.model_validate(snapshot(3, services=[])),
            )
            task = asyncio.create_task(older())
            await wait_blocked(store.factory, await pids.get())
            await newer.commit()
        assert (await asyncio.wait_for(task, 5)).outcome == "stale"
        assert not (await decision(store)).allowed
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_revocation_ahead_of_waiting_writer_invalidates_prior_inspection(store):
    await apply(store, snapshot())
    pids, go = asyncio.Queue(), asyncio.Event()

    async def old_caller():
        async with store.factory() as db:
            cached = await db.get(WorkspaceIntegrationState, store.wid)
            assert (await service.inspect_paid_integration(db, store.wid, SERVICE)).allowed
            await pids.put(await db.scalar(text("SELECT pg_backend_pid()")))
            await go.wait()
            with pytest.raises(AuthorizationError):
                async with service.paid_integration_write(db, store.wid, SERVICE):
                    pytest.fail("Writer must not enter after revocation won the lock")
            # The previously loaded row did not become an authorization cache.
            assert cached is not None

    task = asyncio.create_task(old_caller())
    try:
        pid = await pids.get()
        async with store.factory() as publisher:
            await service.apply_integration_entitlements(
                publisher,
                store.wid,
                IntegrationEntitlementPush.model_validate(snapshot(2, services=[])),
            )
            go.set()
            await wait_blocked(store.factory, pid)
            await publisher.commit()
        await asyncio.wait_for(task, 5)
        assert await context_name(store) == "Original"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# --- #2801: the guard and the public require start the transaction's writes ---

XID = text("SELECT pg_current_xact_id_if_assigned()::text")
EARLIER = ["flushed_insert", "users_for_update", "users_for_key_share"]


async def earlier_action(db, store, action, extra_uid):
    if action == "flushed_insert":
        db.add(User(user_id=extra_uid, email=extra_uid + "@example.test"))
        await db.flush()
        return
    mode = "UPDATE" if action == "users_for_update" else "KEY SHARE"
    await db.execute(text(f"SELECT 1 FROM users WHERE user_id = :u FOR {mode}"), {"u": store.uid})


async def enter(db, store, entry):
    if entry == "guard":
        async with service.paid_integration_write(db, store.wid, SERVICE):
            pytest.fail("The guard admitted a transaction that already wrote or locked a row")
    else:
        await service.require_paid_integration(db, store.wid, SERVICE)


async def workspace_row_is_free(store):
    async with store.factory() as other:
        try:
            locked = await other.scalar(
                select(Workspace.id).where(Workspace.id == store.wid).with_for_update(nowait=True)
            )
        except DBAPIError as exc:
            if "could not obtain lock" not in str(exc):
                raise
            return False
        finally:
            await other.rollback()
    return locked == store.wid


@pytest.mark.parametrize("entry", ["guard", "require"])
@pytest.mark.parametrize("action", EARLIER)
async def test_entry_refuses_a_transaction_that_already_wrote_or_locked_a_row(store, action, entry):
    await apply(store, snapshot())
    extra_uid = "paid-integration-extra-" + uuid4().hex
    try:
        async with store.factory() as db:
            await earlier_action(db, store, action, extra_uid)
            xid = await db.scalar(XID)
            assert xid is not None
            hooks = list(db.sync_session.dispatch.before_commit)
            with pytest.raises(RuntimeError, match="first write or row lock"):
                await enter(db, store, entry)
            # The caller's transaction is left to the caller: not rolled back,
            # no commit hook or guard flag left behind, no workspace row lock.
            assert await db.scalar(XID) == xid
            assert await db.scalar(
                select(func.count()).select_from(User).where(User.user_id == extra_uid)
            ) == int(action == "flushed_insert")
            assert "paid_integration_write" not in db.info
            assert list(db.sync_session.dispatch.before_commit) == hooks
            assert await workspace_row_is_free(store)
            await db.commit()
    finally:
        async with store.factory() as db:
            await db.execute(delete(User).where(User.user_id == extra_uid))
            await db.commit()


async def test_guard_admits_a_read_only_transaction_and_commits(store):
    await apply(store, snapshot())
    async with store.factory() as db:
        assert (await db.get(Context, store.cid)).display_name == "Original"
        assert (await service.inspect_paid_integration(db, store.wid, SERVICE)).allowed
        await db.scalar(select(func.count()).select_from(Workspace))
        assert await db.scalar(XID) is None
        async with service.paid_integration_write(db, store.wid, SERVICE):
            (await db.get(Context, store.cid)).display_name = "Admitted after reads"
    assert await context_name(store) == "Admitted after reads"


async def test_require_admits_a_read_only_transaction_once(store):
    await apply(store, snapshot())
    async with store.factory() as db:
        assert (await service.inspect_paid_integration(db, store.wid, SERVICE)).allowed
        assert (await service.require_paid_integration(db, store.wid, SERVICE)).allowed
        # The admission holds the workspace row lock, so it is one per transaction.
        assert not await workspace_row_is_free(store)
        with pytest.raises(RuntimeError, match="first write or row lock"):
            await service.require_paid_integration(db, store.wid, SERVICE)
        await db.rollback()


async def test_require_inside_the_guard_refuses_and_rolls_the_guard_back(store):
    await apply(store, snapshot())
    async with store.factory() as db:
        with pytest.raises(RuntimeError, match="first write or row lock"):
            async with service.paid_integration_write(db, store.wid, SERVICE):
                (await db.get(Context, store.cid)).display_name = "Must roll back"
                await service.require_paid_integration(db, store.wid, SERVICE)
        assert "paid_integration_write" not in db.info
    assert await context_name(store) == "Original"
