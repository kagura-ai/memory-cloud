"""Transaction fences: concurrent paid activation, consent withdrawal and ownership."""

import asyncio
from contextlib import asynccontextmanager
from time import monotonic
from uuid import UUID

import pytest
from sqlalchemy import select, text

from models.auth import User
from models.integration_entitlement_schemas import IntegrationEntitlementPush
from services import integration_entitlements
from services import sync_installations as service
from tests.integration import sync_installation_fixtures as fixtures
from tests.integration.sync_installation_fixtures import (
    activation,
    approval_body,
    approve,
    http,
    paid,
    targets,
    transfer,
)
from utils.exceptions import ConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store


async def paused_provision(store, monkeypatch, approval):
    entered, finish = asyncio.Event(), asyncio.Event()
    original = service.create_sync_target
    calls = []

    async def paused(*args):
        calls.append(True)
        result = await original(*args)
        entered.set()
        await finish.wait()
        return result

    monkeypatch.setattr(service, "create_sync_target", paused)
    task = asyncio.create_task(http(store, "POST", "", billing=True, body=activation(approval)))
    await asyncio.wait_for(entered.wait(), 5)
    return task, finish, calls


async def cleanup(task, finish):
    finish.set()
    if not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def wait_blocked(store, pid):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        async with store.factory() as observer:
            blocked = await asyncio.wait_for(
                observer.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
                ),
                2,
            )
        if blocked:
            return
        await asyncio.sleep(0.01)
    pytest.fail("Expected PostgreSQL Workspace lock wait was not observed")


async def test_concurrent_activations_serialize_to_the_same_committed_identity(store, monkeypatch):
    approval = await approve(store)
    await paid(store)
    pids = asyncio.Queue()
    original_guard = service.paid_integration_write

    @asynccontextmanager
    async def observe_entry(db, wid, service_id):
        await pids.put(await db.scalar(text("SELECT pg_backend_pid()")))
        async with original_guard(db, wid, service_id) as access:
            yield access

    monkeypatch.setattr(service, "paid_integration_write", observe_entry)
    task, finish, calls = await paused_provision(store, monkeypatch, approval)
    await asyncio.wait_for(pids.get(), 5)
    contender_task = None
    try:
        contender_task = asyncio.create_task(
            http(store, "POST", "", billing=True, body=activation(approval))
        )
        pid = await asyncio.wait_for(pids.get(), 5)
        await wait_blocked(store, pid)
        assert await targets(store) == (0, 0, 0)  # uncommitted target is not partially visible
        finish.set()
        first = await asyncio.wait_for(task, 5)
        assert first.status_code == 200, first.text
        contender = await asyncio.wait_for(contender_task, 5)
        assert contender.status_code == 200 and contender.json() == first.json()
        replay = await http(store, "POST", "", billing=True, body=activation(approval))
        assert replay.json() == first.json() and len(calls) == 1
        assert await targets(store) == (1, 1, 1)
    finally:
        await cleanup(task, finish)
        if contender_task is not None and not contender_task.done():
            contender_task.cancel()
            await asyncio.gather(contender_task, return_exceptions=True)


async def test_activation_owns_the_boundary_before_a_concurrent_transfer(store, monkeypatch):
    approval = await approve(store)
    await paid(store)
    task, finish, _ = await paused_provision(store, monkeypatch, approval)
    try:
        with pytest.raises(ConflictError):
            await transfer(store, "next")
        finish.set()
        result = await asyncio.wait_for(task, 5)
        assert result.status_code == 200
        await transfer(store, "next")
        suffix = "sync-installations/" + result.json()["installation_id"]
        assert (await http(store, "DELETE", suffix, role="owner")).status_code == 404
        assert (await http(store, "GET", suffix, role="next")).json()[
            "installation_id"
        ] == result.json()["installation_id"]
    finally:
        await cleanup(task, finish)


async def test_activation_and_consent_withdrawal_have_a_serialized_boundary(store, monkeypatch):
    approval = await approve(store)
    await paid(store)
    task, finish, _ = await paused_provision(store, monkeypatch, approval)
    try:
        suffix = "sync-authorizations/" + approval["authorization_id"]
        assert (await http(store, "DELETE", suffix)).status_code == 409
        finish.set()
        result = await asyncio.wait_for(task, 5)
        assert result.status_code == 200
        assert (await http(store, "DELETE", suffix)).status_code == 409  # already consumed
        assert (
            await http(store, "DELETE", "sync-installations/" + result.json()["installation_id"])
        ).json()["generation"] == 2
    finally:
        await cleanup(task, finish)


async def test_busy_erasure_user_lock_returns_retryable_conflict_without_partial_changes(store):
    async with store.factory() as erasure:
        await erasure.scalar(
            select(User).where(User.user_id == store.users["owner"]).with_for_update()
        )
        response = await http(store, "POST", "sync-authorizations", body=approval_body())
        assert response.status_code == 409, response.text
    assert await targets(store) == (0, 0, 0)


async def test_pending_old_authorization_cannot_be_reused_after_transfer_wins(store):
    approval = await approve(store)
    await paid(store)
    await transfer(store, "next")
    response = await http(store, "POST", "", billing=True, body=activation(approval))
    assert response.status_code == 409 and await targets(store) == (0, 0, 0)
    # A fresh request, explicitly approved by the current owner, is required.
    fresh = await approve(store, role="next")
    response = await http(store, "POST", "sync-installations", role="next", body=activation(fresh))
    assert response.status_code == 201
    assert UUID(response.json()["installation_id"]) is not None


async def test_paid_guard_is_the_first_write_and_row_lock_boundary(store, monkeypatch):
    approval = await approve(store)
    await paid(store)
    original = service.paid_integration_write
    observed = []

    @asynccontextmanager
    async def strict_entry(db, wid, service_id):
        # A prior row lock or flushed write assigns an xid even when ORM state
        # is clean. This is the security contract being enforced by #2801.
        assert await db.scalar(text("SELECT pg_current_xact_id_if_assigned()")) is None
        observed.append(True)
        async with original(db, wid, service_id) as access:
            yield access

    monkeypatch.setattr(service, "paid_integration_write", strict_entry)
    response = await http(store, "POST", "", billing=True, body=activation(approval))
    assert response.status_code == 200 and observed == [True], response.text


@pytest.mark.parametrize("first", ["activation", "publisher"])
async def test_paid_revocation_and_target_creation_have_one_serialized_boundary(
    store, monkeypatch, first
):
    approval = await approve(store)
    await paid(store)
    pids = asyncio.Queue()
    original_guard = service.paid_integration_write

    @asynccontextmanager
    async def observe_entry(db, wid, service_id):
        await pids.put(await db.scalar(text("SELECT pg_backend_pid()")))
        async with original_guard(db, wid, service_id) as access:
            yield access

    monkeypatch.setattr(service, "paid_integration_write", observe_entry)
    revoked = IntegrationEntitlementPush(
        revision=2,
        event_id="revoke-2",
        source_ref="authoritative-billing",
        max_access=None,
        services=[],
    )
    if first == "publisher":
        task = None
        try:
            async with store.factory() as publisher:
                await integration_entitlements.apply_integration_entitlements(
                    publisher, store.wid, revoked
                )
                task = asyncio.create_task(
                    http(store, "POST", "", billing=True, body=activation(approval))
                )
                await wait_blocked(store, await asyncio.wait_for(pids.get(), 5))
                await publisher.commit()
            response = await asyncio.wait_for(task, 5)
            assert response.status_code == 404 and await targets(store) == (0, 0, 0)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return
    task, finish, _ = await paused_provision(store, monkeypatch, approval)
    await asyncio.wait_for(pids.get(), 5)
    publisher_pids = asyncio.Queue()

    async def publish_revocation():
        async with store.factory() as publisher:
            await publisher_pids.put(await publisher.scalar(text("SELECT pg_backend_pid()")))
            await integration_entitlements.apply_integration_entitlements(
                publisher, store.wid, revoked
            )
            await publisher.commit()

    publish_task = asyncio.create_task(publish_revocation())
    try:
        await wait_blocked(store, await asyncio.wait_for(publisher_pids.get(), 5))
        finish.set()
        assert (await asyncio.wait_for(task, 5)).status_code == 200
        await asyncio.wait_for(publish_task, 5)
        assert await targets(store) == (1, 1, 1)
        async with store.factory() as db:
            assert not (
                await integration_entitlements.inspect_paid_integration(
                    db, store.wid, store.service_id
                )
            ).allowed
    finally:
        await cleanup(task, finish)
        if not publish_task.done():
            publish_task.cancel()
            await asyncio.gather(publish_task, return_exceptions=True)
