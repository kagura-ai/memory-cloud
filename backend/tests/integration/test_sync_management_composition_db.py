"""Lifecycle access, guarded stop/finalize and receipt lookup share one fence (#2871 #2873 #2875).

Each operation is held inside its own authoritative transaction while the other two run on
fresh sessions. The held operation's locks make every concurrent one fail fast with the fixed
409 instead of waiting, deadlocking, reporting a typed absence or observing a half transition.
"""

from uuid import UUID, uuid4

import pytest

from services import sync_management, sync_provisioning, sync_resume_receipt
from services.sync_managed_guard import target_fence
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import human

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, managed = (
    management.store,
    management.worker,
    management.feeds,
    management.managed,
)
STATE = ("mode", "epoch", "generation", "stop_event_id")


async def expected_state(subject):
    result = await human(subject, "GET", "management")
    assert result.status_code == 200, result.text
    return {key: result.json()[key] for key in STATE}


def lookup(subject):
    return {
        "operation_id": str(uuid4()),
        "preview_id": str(uuid4()),
        "diff_hash": "b" * 64,
        "original_subject": subject.store.users["owner"],
    }


async def access(subject):
    return await human(subject, "GET", "management/access")


async def receipt(subject, body):
    return await human(subject, "POST", "management/resume/receipt", body=body)


async def control(subject, action, body):
    return await human(subject, "POST", "management/" + action, body=body)


def busy(response):
    """Contention is an error: no capability, observation time, absence or receipt."""
    assert response.status_code == 409, response.text
    assert response.json() == {"error": "RES-002"}
    assert response.headers["cache-control"] == "no-store"
    for marker in ("permissions", "as_of", "not_found", "mode"):
        assert marker not in response.text


@pytest.mark.parametrize("action", ["stop", "finalize"])
async def test_in_flight_guarded_transition_fences_access_and_receipt_until_it_commits(
    managed, monkeypatch, action
):
    if action == "finalize":
        await management.stop(managed)
    body = {"operation_id": str(uuid4()), "expected": await expected_state(managed)}
    question = lookup(managed)
    name = "finish_stopping" if action == "finalize" else "start_stopping"
    original = getattr(sync_management, name)
    seen = {}

    async def observed_mid_transition(*args, **kwargs):
        row = await original(*args, **kwargs)
        # Flushed, not committed: the readers must neither wait nor see this state.
        seen["access"] = await access(managed)
        seen["receipt"] = await receipt(managed, question)
        return row

    monkeypatch.setattr(sync_management, name, observed_mid_transition)
    result = await control(managed, action, body)
    monkeypatch.setattr(sync_management, name, original)
    assert result.status_code == 200, result.text
    assert set(seen) == {"access", "receipt"}
    for response in seen.values():
        busy(response)
    profile = await access(managed)
    assert profile.status_code == 200, profile.text
    assert profile.json()["management"] == result.json()
    assert profile.json()["target"]["generation"] == result.json()["generation"]
    after = await receipt(managed, question)
    assert after.status_code == 200 and after.json()["result"] == "not_found", after.text


@pytest.mark.parametrize("action", ["stop", "finalize"])
async def test_in_flight_access_read_fences_guarded_controls_and_receipt(
    managed, monkeypatch, action
):
    if action == "finalize":
        await management.stop(managed)
    expected = await expected_state(managed)
    body = {"operation_id": str(uuid4()), "expected": expected}
    original = sync_provisioning.installation_view
    seen = {}

    async def observed_mid_read(*args, **kwargs):
        seen["control"] = await control(managed, action, body)
        seen["receipt"] = await receipt(managed, lookup(managed))
        return await original(*args, **kwargs)

    monkeypatch.setattr(sync_provisioning, "installation_view", observed_mid_read)
    profile = await access(managed)
    monkeypatch.setattr(sync_provisioning, "installation_view", original)
    assert profile.status_code == 200, profile.text
    assert {key: profile.json()["management"][key] for key in STATE} == expected
    for response in seen.values():
        busy(response)
    # The refused attempt left no receipt: the same operation now applies once.
    assert await expected_state(managed) == expected
    applied = await control(managed, action, body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["mode"] == ("manual" if action == "finalize" else "stopping")


@pytest.mark.parametrize("action", ["stop", "finalize"])
async def test_in_flight_receipt_lookup_fences_guarded_controls_and_access(
    managed, monkeypatch, action
):
    if action == "finalize":
        await management.stop(managed)
    expected = await expected_state(managed)
    body = {"operation_id": str(uuid4()), "expected": expected}
    question = lookup(managed)
    original = sync_resume_receipt.locked
    seen = {}

    async def observed_mid_lookup(db, model, *conditions, **kwargs):
        row = await original(db, model, *conditions, **kwargs)
        if not seen:
            seen["control"] = await control(managed, action, body)
            seen["access"] = await access(managed)
        return row

    monkeypatch.setattr(sync_resume_receipt, "locked", observed_mid_lookup)
    observed = await receipt(managed, question)
    monkeypatch.setattr(sync_resume_receipt, "locked", original)
    assert observed.status_code == 200 and observed.json()["result"] == "not_found", observed.text
    assert set(seen) == {"control", "access"}
    for response in seen.values():
        busy(response)
    assert await expected_state(managed) == expected
    applied = await control(managed, action, body)
    assert applied.status_code == 200, applied.text


async def test_held_exclusive_management_fence_is_busy_for_every_lifecycle_operation(managed):
    body = {"operation_id": str(uuid4()), "expected": await expected_state(managed)}
    question = lookup(managed)
    async with managed.factory() as transition:
        await target_fence(transition, UUID(managed.target["context_id"]), exclusive=True)
        busy(await access(managed))
        busy(await receipt(managed, question))
        busy(await control(managed, "stop", body))
        await transition.rollback()
    assert (await access(managed)).status_code == 200
    after = await receipt(managed, question)
    assert after.status_code == 200 and after.json()["result"] == "not_found", after.text
    assert (await control(managed, "stop", body)).status_code == 200
