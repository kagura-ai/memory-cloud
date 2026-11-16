"""Real management confirmation fences and immutable guarded operation identity."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from models.auth import AuditLog, Context, WorkspaceMember
from models.sync_feed import SyncChunk, SyncFeed, SyncRun
from models.sync_installation import SyncInstallation
from models.sync_management import SyncManagedTarget, SyncModeOperation
from services.sync_managed_guard import target_fence
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import human, product, staged

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, managed = (
    management.store,
    management.worker,
    management.feeds,
    management.managed,
)


async def expected_state(subject):
    result = await human(subject, "GET", "management")
    assert result.status_code == 200, result.text
    return {key: result.json()[key] for key in ("mode", "epoch", "generation", "stop_event_id")}


async def footprint(subject):
    """No body/credential copies; a refusal must preserve mutation evidence."""
    async with subject.factory() as db:
        installation = await db.get(SyncInstallation, subject.iid)
        target = await db.get(SyncManagedTarget, subject.iid)
        context = await db.get(Context, UUID(subject.target["context_id"]))
        runs = (
            await db.execute(
                select(SyncRun.id, SyncRun.state, SyncRun.diff_hash, SyncRun.approval_id)
                .join(SyncFeed)
                .where(SyncFeed.installation_id == subject.iid)
                .order_by(SyncRun.id)
            )
        ).all()
        chunks = (
            await db.execute(
                select(
                    SyncChunk.run_id,
                    SyncChunk.chunk_index,
                    SyncChunk.digest,
                    SyncChunk.records.is_not(None),
                )
                .join(SyncRun)
                .join(SyncFeed)
                .where(SyncFeed.installation_id == subject.iid)
                .order_by(SyncChunk.run_id, SyncChunk.chunk_index)
            )
        ).all()
        return {
            "generation": installation.generation,
            "target": None
            if target is None
            else tuple(
                getattr(target, key)
                for key in (
                    "mode",
                    "epoch",
                    "generation",
                    "stop_event_id",
                    "event_floor",
                    "drain_generation",
                    "updated_at",
                )
            ),
            "sleep": context.sleep_mode,
            "runs": runs,
            "chunks": chunks,
            "receipts": await db.scalar(
                select(func.count())
                .select_from(SyncModeOperation)
                .where(SyncModeOperation.installation_id == subject.iid)
            ),
            # Authenticated denials remain auditable in their separate boundary.
            "transitions": await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.resource == "sync:" + str(subject.iid),
                    AuditLog.action.like("sync.management.%"),
                )
            ),
        }


def private_conflict(response):
    assert response.status_code == 409, response.text
    assert response.json() == {"error": "RES-002"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


async def test_guarded_stop_and_finalize_accept_the_confirmed_current_state(managed):
    first = await expected_state(managed)
    stopped = await human(
        managed, "POST", "management/stop", body={"operation_id": str(uuid4()), "expected": first}
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["mode"] == "stopping"
    assert stopped.json()["generation"] == first["generation"] + 1
    current = await expected_state(managed)
    finished = await human(
        managed,
        "POST",
        "management/finalize",
        body={"operation_id": str(uuid4()), "expected": current},
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["mode"] == "manual"
    assert finished.json()["generation"] == current["generation"]
    assert finished.json()["epoch"] == current["epoch"] + 1


@pytest.mark.parametrize("action", ["stop", "finalize"])
@pytest.mark.parametrize("field", ["mode", "epoch", "generation", "stop_event_id"])
async def test_each_stale_state_field_refuses_without_transition_receipt_or_staging_changes(
    managed, action, field
):
    if action == "stop":
        await staged(managed, [product()])
    else:
        await management.stop(managed)
    expected = await expected_state(managed)
    expected[field] = (
        ("manual" if expected[field] != "manual" else "feed")
        if field == "mode"
        else expected[field] + 1
    )
    before = await footprint(managed)
    result = await human(
        managed,
        "POST",
        "management/" + action,
        body={"operation_id": str(uuid4()), "expected": expected},
    )
    private_conflict(result)
    assert await footprint(managed) == before


async def test_old_guard_cannot_stop_a_resumed_feed_generation_or_purge_its_new_pending_run(
    managed,
):
    original = await expected_state(managed)
    stop_body = {"operation_id": str(uuid4()), "expected": original}
    stopped = await human(managed, "POST", "management/stop", body=stop_body)
    assert stopped.status_code == 200, stopped.text
    finalize_body = {"operation_id": str(uuid4()), "expected": await expected_state(managed)}
    finalized = await human(managed, "POST", "management/finalize", body=finalize_body)
    assert finalized.status_code == 200, finalized.text
    await management.pin(managed, [product()])
    preview = await human(managed, "POST", "management/resume/preview")
    assert preview.status_code == 200, preview.text
    resumed = await human(
        managed,
        "POST",
        "management/resume/confirm",
        body={
            "operation_id": str(uuid4()),
            "preview_id": preview.json()["preview_id"],
            "diff_hash": preview.json()["diff_hash"],
        },
    )
    assert resumed.status_code == 200 and resumed.json()["mode"] == "feed"
    assert resumed.json()["generation"] > original["generation"]
    await management.mint(managed)
    await staged(managed, [product(title="Later pending source")])
    before = await footprint(managed)
    for action, body, receipt in (
        ("stop", stop_body, stopped.json()),
        ("finalize", finalize_body, finalized.json()),
    ):
        replay = await human(managed, "POST", "management/" + action, body=body)
        assert replay.status_code == 200 and replay.json() == receipt, replay.text
        assert (await expected_state(managed))["mode"] == "feed"
        assert await footprint(managed) == before
    result = await human(
        managed,
        "POST",
        "management/stop",
        body={"operation_id": str(uuid4()), "expected": original},
    )
    private_conflict(result)
    assert await footprint(managed) == before


async def test_exact_guarded_replay_is_historical_and_changed_guard_or_legacy_reuse_conflicts(
    managed,
):
    expected = await expected_state(managed)
    body = {"operation_id": str(uuid4()), "expected": expected}
    first = await human(managed, "POST", "management/stop", body=body)
    assert first.status_code == 200, first.text
    await management.finalize(managed)
    before = await footprint(managed)
    replay = await human(managed, "POST", "management/stop", body=body)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert (await expected_state(managed))["mode"] == "manual"
    private_conflict(
        await human(
            managed,
            "POST",
            "management/stop",
            body={**body, "expected": await expected_state(managed)},
        )
    )
    private_conflict(
        await human(managed, "POST", "management/stop", body={"operation_id": body["operation_id"]})
    )
    assert await footprint(managed) == before


async def test_legacy_omission_and_null_keep_their_original_operation_identity(managed):
    body = {"operation_id": str(uuid4())}
    first = await human(managed, "POST", "management/stop", body=body)
    assert first.status_code == 200, first.text
    same = await human(managed, "POST", "management/stop", body={**body, "expected": None})
    assert same.status_code == 200 and same.json() == first.json()
    assert (await management.finalize(managed))["mode"] == "manual"


@pytest.mark.parametrize("stale", [True, False])
async def test_absent_ownership_uses_the_read_only_default_without_enrolling_a_stale_guard(
    managed, stale
):
    async with managed.factory() as db:
        await db.delete(await db.get(SyncManagedTarget, managed.iid))
        await db.commit()
    expected = await expected_state(managed)
    before = await footprint(managed)
    assert before["target"] is None
    assert expected == {
        "mode": "feed",
        "epoch": 1,
        "generation": before["generation"],
        "stop_event_id": 0,
    }
    if stale:
        expected["generation"] += 1
    result = await human(
        managed,
        "POST",
        "management/stop",
        body={"operation_id": str(uuid4()), "expected": expected},
    )
    if stale:
        private_conflict(result)
        assert await footprint(managed) == before
    else:
        assert result.status_code == 200 and result.json()["mode"] == "stopping", result.text
        assert result.json()["generation"] == before["generation"] + 1


@pytest.mark.parametrize("action", ["stop", "finalize"])
async def test_guarded_controls_refuse_an_inflight_target_writer_without_waiting_or_side_effects(
    managed, action
):
    if action == "finalize":
        await management.stop(managed)
    body = {"operation_id": str(uuid4()), "expected": await expected_state(managed)}
    before = await footprint(managed)
    async with managed.factory() as writer:
        await target_fence(writer, UUID(managed.target["context_id"]))
        private_conflict(await human(managed, "POST", "management/" + action, body=body))
        await writer.rollback()
    assert await footprint(managed) == before
    result = await human(managed, "POST", "management/" + action, body=body)
    assert result.status_code == 200, result.text


@pytest.mark.parametrize("action", ["stop", "finalize"])
@pytest.mark.parametrize("role", ["viewer", "outsider"])
async def test_a_current_snapshot_never_substitutes_for_current_editor_authority(
    managed, action, role
):
    if action == "finalize":
        await management.stop(managed)
    before = await footprint(managed)
    result = await human(
        managed,
        "POST",
        "management/" + action,
        role=role,
        body={"operation_id": str(uuid4()), "expected": await expected_state(managed)},
    )
    assert result.status_code == 404, result.text
    assert result.headers["cache-control"] == "no-store"
    assert await footprint(managed) == before


async def test_a_guarded_receipt_needs_the_original_actor_and_current_authority_on_replay(managed):
    body = {"operation_id": str(uuid4()), "expected": await expected_state(managed)}
    first = await human(managed, "POST", "management/stop", role="member", body=body)
    assert first.status_code == 200, first.text
    before = await footprint(managed)
    private_conflict(await human(managed, "POST", "management/stop", body=body))
    async with managed.factory() as db:
        member = await db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == managed.store.wid,
                WorkspaceMember.user_id == managed.store.users["member"],
            )
        )
        member.role = "viewer"
        await db.commit()
    denied = await human(managed, "POST", "management/stop", role="member", body=body)
    assert denied.status_code == 404, denied.text
    assert await footprint(managed) == before


async def test_a_legacy_operation_id_cannot_be_upgraded_into_a_guarded_operation(managed):
    body = {"operation_id": str(uuid4())}
    first = await human(managed, "POST", "management/stop", body=body)
    assert first.status_code == 200, first.text
    before = await footprint(managed)
    private_conflict(
        await human(
            managed,
            "POST",
            "management/stop",
            body={**body, "expected": await expected_state(managed)},
        )
    )
    assert await footprint(managed) == before


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"mode": "feed"},
        {"mode": "feed", "epoch": True, "generation": 1, "stop_event_id": 0},
        {"mode": "never-echo-management-input", "epoch": 1, "generation": 1, "stop_event_id": 0},
        {"mode": "feed", "epoch": 1, "generation": 1, "stop_event_id": -1},
        {"mode": "feed", "epoch": 1, "generation": 1, "stop_event_id": 0, "skip_lock": True},
    ],
)
async def test_malformed_expected_state_never_becomes_an_unguarded_control(managed, invalid):
    before = await footprint(managed)
    result = await human(
        managed, "POST", "management/stop", body={"operation_id": str(uuid4()), "expected": invalid}
    )
    assert result.status_code == 400, result.text
    assert result.headers["cache-control"] == "no-store"
    assert "never-echo-management-input" not in result.text
    assert await footprint(managed) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", 0),
        ("generation", False),
        ("generation", "1"),
        ("epoch", 1.0),
        ("stop_event_id", True),
        ("generation", 2**53),
        ("epoch", 2**53),
        ("stop_event_id", 2**53),
    ],
)
async def test_strict_numeric_guard_bounds_never_coerce_or_overflow(managed, field, value):
    expected = {**await expected_state(managed), field: value}
    before = await footprint(managed)
    result = await human(
        managed,
        "POST",
        "management/finalize",
        body={"operation_id": str(uuid4()), "expected": expected},
    )
    assert result.status_code == 400 and result.json() == {"error": "invalid_request"}, result.text
    assert await footprint(managed) == before
