"""Real transaction fences, current human consent and late-worker suppression."""

import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from api.routes import sync_management as routes
from models.auth import Context, Workspace
from models.deferred_point_delete import DeferredPointDelete
from models.memory import GraphMemory, Memory, MemoryRevision
from models.resource import IndexerState, ResourceEvent, ResourceSchema
from models.sync_feed import SyncRun
from models.sync_installation import SyncInstallation
from models.sync_management import (
    SyncManagedTarget,
    SyncManualRetirement,
    SyncModeOperation,
    SyncResumeCandidate,
    SyncResumePreview,
)
from repositories.memory import MemoryRepository
from services import resource_indexer, sync_management, sync_resume
from services.sync_feed_provenance import guard_sync_event
from services.sync_managed_guard import assert_context_mutable, target_fence
from tests.integration import sync_credential_fixtures as credentials
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration import sync_installation_fixtures as installations
from tests.integration.sync_feed_fixtures import committed, human, product, service, staged
from utils.exceptions import ConflictError, ManagedDataLockedError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


@pytest_asyncio.fixture(loop_scope="session")
async def managed(feeds, monkeypatch):
    monkeypatch.setattr(sync_management, "_maintenance_after", None)
    monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", True)
    for module in (sync_management, sync_resume):
        monkeypatch.setattr(module, "utcnow", lambda: feeds.store.clock.now)
    feeds.store.app.include_router(routes.router, prefix="/api/v1")
    async with feeds.factory() as db:
        installation = await db.get(SyncInstallation, feeds.iid)
        await sync_management.enroll(db, installation)
        await db.commit()
    result = await human(feeds, "GET", "management")
    assert result.status_code == 200, result.text
    assert result.json()["mode"] == "feed"
    feeds.management_extra_workspaces = []
    yield feeds
    if feeds.management_extra_workspaces:
        async with feeds.factory() as db:
            await db.execute(
                delete(Workspace).where(Workspace.id.in_(feeds.management_extra_workspaces))
            )
            await db.commit()


async def stop(worker):
    result = await human(worker, "POST", "management/stop", body={"operation_id": str(uuid4())})
    assert result.status_code == 200, result.text
    return result.json()


async def finalize(worker):
    result = await human(worker, "POST", "management/finalize", body={"operation_id": str(uuid4())})
    assert result.status_code == 200, result.text
    return result.json()


async def manual(worker):
    await stop(worker)
    return await finalize(worker)


async def mint(worker):
    worker.token = (await credentials.mint(worker))["access_token"]


async def pin(worker, records):
    await mint(worker)
    row = await staged(worker, records)
    result = await service(worker, "POST", "resume-candidates", body={"run_id": row["run_id"]})
    assert result.status_code == 200, result.text
    return result.json()


async def apply_pending(worker, monkeypatch):
    embed = AsyncMock()
    embed.embed.return_value = [0.1] * 1536
    qdrant = MagicMock()
    qdrant.upsert, qdrant.delete = AsyncMock(), AsyncMock()
    monkeypatch.setattr(resource_indexer, "get_qdrant_client", lambda: qdrant)
    async with worker.factory() as db:
        context = await db.get(Context, UUID(worker.target["context_id"]))
        schema = await db.scalar(
            select(ResourceSchema).where(
                ResourceSchema.resource_pk == UUID(worker.target["resource_pk"])
            )
        )
        events = (
            await db.scalars(
                select(ResourceEvent)
                .where(ResourceEvent.resource_pk == UUID(worker.target["resource_pk"]))
                .order_by(ResourceEvent.id)
            )
        ).all()
        indexer = resource_indexer.ResourceIndexer(db)
        for event in events:
            if await guard_sync_event(db, event, context):
                if event.op == "upsert":
                    await indexer._apply_upsert(event, schema, context, "test-collection", embed)
                else:
                    await indexer._apply_delete(event, context, "test-collection")
        state = await db.scalar(
            select(IndexerState).where(
                IndexerState.resource_pk == schema.resource_pk,
                IndexerState.context_id == context.id,
            )
        )
        if state is None:
            state = IndexerState(
                resource_pk=schema.resource_pk,
                resource_id=context.resource_id,
                context_id=context.id,
            )
            db.add(state)
        state.last_offset = max([event.id for event in events], default=0)
        state.metrics = {}
        await db.commit()
    return qdrant


async def test_feed_pause_failures_and_disabled_rollout_keep_generic_writes_locked(
    managed, monkeypatch
):
    ctx = UUID(managed.target["context_id"])
    for enabled in (True, False):
        monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", enabled)
        async with managed.factory() as db:
            with pytest.raises(ManagedDataLockedError):
                await assert_context_mutable(db, ctx)
    async with managed.factory() as db:
        installation = await db.get(SyncInstallation, managed.iid)
        installation.state = "paused"
        await db.commit()
    async with managed.factory() as db:
        with pytest.raises(ManagedDataLockedError):
            await assert_context_mutable(db, ctx)


async def test_stop_is_idempotent_generation_fenced_and_crash_retains_lock(managed):
    pending = await staged(managed, [product()])
    op = {"operation_id": str(uuid4())}
    first = await human(managed, "POST", "management/stop", body=op)
    again = await human(managed, "POST", "management/stop", body=op)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert first.json()["generation"] == 2
    async with managed.factory() as db:
        assert (await db.get(SyncManagedTarget, managed.iid)).mode == "stopping"
        with pytest.raises(ManagedDataLockedError):
            await assert_context_mutable(db, UUID(managed.target["context_id"]))
    result = await service(
        managed,
        "POST",
        f"runs/{pending['run_id']}/commit",
        body={"diff_hash": pending["diff_hash"]},
    )
    assert result.status_code in (401, 403, 409)
    assert (await finalize(managed))["mode"] == "manual"
    async with managed.factory() as db:
        await assert_context_mutable(db, UUID(managed.target["context_id"]))


async def test_transition_refuses_inflight_writer_and_absent_row_enrollment_race(managed):
    ctx = UUID(managed.target["context_id"])
    async with managed.factory() as writer:
        await target_fence(writer, ctx)
        response = await human(
            managed, "POST", "management/stop", body={"operation_id": str(uuid4())}
        )
        assert response.status_code == 409
        await writer.rollback()
    assert (await stop(managed))["mode"] == "stopping"


async def test_drain_requires_real_indexed_postconditions_not_just_cursor(managed, monkeypatch):
    await committed(managed, [product()])
    stopped = await stop(managed)
    result = await human(
        managed, "POST", "management/finalize", body={"operation_id": str(uuid4())}
    )
    assert result.status_code == 409
    async with managed.factory() as db:
        db.add(
            IndexerState(
                resource_pk=UUID(managed.target["resource_pk"]),
                resource_id=managed.target["resource_id"],
                context_id=UUID(managed.target["context_id"]),
                last_offset=stopped["stop_event_id"],
            )
        )
        await db.commit()
    result = await human(
        managed, "POST", "management/finalize", body={"operation_id": str(uuid4())}
    )
    assert result.status_code == 409
    await apply_pending(managed, monkeypatch)
    assert (await finalize(managed))["mode"] == "manual"
    async with managed.factory() as db:
        memory = await db.scalar(
            select(Memory).where(
                Memory.context_id == UUID(managed.target["context_id"]), Memory.deleted_at.is_(None)
            )
        )
        memory.content = json.dumps({"title": "Manual changed"})
        memory.version += 1
        await MemoryRepository(db).update(memory.id, memory)
        await db.commit()
    # A replayed old source event is suppressed before touching the manual row or vectors.
    async with managed.factory() as db:
        event = await db.scalar(
            select(ResourceEvent).where(
                ResourceEvent.resource_pk == UUID(managed.target["resource_pk"])
            )
        )
        context = await db.get(Context, UUID(managed.target["context_id"]))
        assert await guard_sync_event(db, event, context) is False


async def test_manual_resume_needs_current_diff_then_reuses_target_and_tombstones_history(
    managed, monkeypatch
):
    await committed(managed, [product()])
    await apply_pending(managed, monkeypatch)
    await manual(managed)
    await pin(managed, [product(title="Fresh source")])
    extra_run = await staged(managed, [product(title="Unapproved parallel upload")])
    preview = await human(managed, "POST", "management/resume/preview")
    assert preview.status_code == 200, preview.text
    p = preview.json()
    diff = await human(
        managed, "GET", "management/resume/diff", params={"preview_id": p["preview_id"], "limit": 1}
    )
    assert diff.status_code == 200 and len(diff.json()["items"]) == 1
    body = {
        "operation_id": str(uuid4()),
        "preview_id": p["preview_id"],
        "diff_hash": p["diff_hash"],
    }
    result = await human(managed, "POST", "management/resume/confirm", body=body)
    assert result.status_code == 200, result.text
    assert result.json()["mode"] == "feed" and result.json()["generation"] == 3
    assert (
        await human(managed, "POST", "management/resume/confirm", body=body)
    ).json() == result.json()
    await apply_pending(managed, monkeypatch)
    async with managed.factory() as db:
        memories = (
            await db.scalars(
                select(Memory).where(Memory.context_id == UUID(managed.target["context_id"]))
            )
        ).all()
        assert len(memories) == 2
        assert sum(memory.deleted_at is None for memory in memories) == 1
        assert "Fresh source" in next(
            memory.content for memory in memories if memory.deleted_at is None
        )
        installation = await db.get(SyncInstallation, managed.iid)
        assert (await db.get(SyncRun, UUID(extra_run["run_id"]))).state == "aborted"
        assert str(installation.resource_pk) == managed.target["resource_pk"]
        assert str(installation.endpoint_id) == managed.target["endpoint_id"]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SyncResumeCandidate)
                .where(SyncResumeCandidate.records.is_not(None))
            )
            == 0
        )


@pytest.mark.parametrize("change", ["manual", "source", "expiry", "config"])
async def test_changed_resume_snapshot_invalidates_human_confirmation(managed, change):
    await manual(managed)
    await pin(managed, [product()])
    result = await human(managed, "POST", "management/resume/preview")
    assert result.status_code == 200, result.text
    p = result.json()
    if change == "manual":
        async with managed.factory() as db:
            memory = Memory(
                user_id=managed.store.users["owner"],
                workspace_id=managed.store.wid,
                context_id=UUID(managed.target["context_id"]),
                summary="Manual",
                content="Keep history",
                type="note",
                client="pytest",
            )
            await MemoryRepository(db).create(memory)
            await db.commit()
    elif change == "source":
        await pin(managed, [product(title="Changed source")])
    elif change == "expiry":
        managed.store.clock.now += timedelta(minutes=15)
    else:
        response = await human(
            managed,
            "PUT",
            f"feeds/{managed.fid}",
            body={"kind": "product", "config_hash": "a" * 64},
        )
        assert response.status_code == 200
    result = await human(
        managed,
        "POST",
        "management/resume/confirm",
        body={
            "operation_id": str(uuid4()),
            "preview_id": p["preview_id"],
            "diff_hash": p["diff_hash"],
        },
    )
    assert result.status_code in (401, 409), result.text
    async with managed.factory() as db:
        assert (await db.get(SyncManagedTarget, managed.iid)).mode == "manual"


@pytest.mark.parametrize(
    "role,allowed", [("owner", True), ("member", True), ("viewer", False), ("outsider", False)]
)
async def test_stop_requires_current_normal_editor_authority(managed, role, allowed):
    result = await human(
        managed, "POST", "management/stop", role=role, body={"operation_id": str(uuid4())}
    )
    assert result.status_code == (200 if allowed else 404), result.text


async def test_unlink_reconnect_retains_target_and_requires_new_owner_consent(managed):
    response = await installations.http(
        managed.store, "DELETE", f"sync-installations/{managed.iid}"
    )
    assert response.status_code == 200, response.text
    async with managed.factory() as db:
        assert (await db.get(SyncManagedTarget, managed.iid)).mode == "stopping"
    await finalize(managed)
    approval = await installations.approve(managed.store)
    command = {"operation_id": str(uuid4()), "authorization_id": approval["authorization_id"]}
    forbidden = await human(managed, "POST", "management/reconnect", body=command, role="member")
    assert forbidden.status_code == 404
    result = await human(managed, "POST", "management/reconnect", body=command)
    assert result.status_code == 200, result.text
    assert result.json()["mode"] == "manual"
    assert (
        await human(managed, "POST", "management/reconnect", body=command)
    ).json() == result.json()
    async with managed.factory() as db:
        row = await db.get(SyncInstallation, managed.iid)
        assert row.state == "active" and row.revoked_at is None
        assert str(row.resource_pk) == managed.target["resource_pk"]
    await mint(managed)
    run = await staged(managed, [product()])
    result = await service(
        managed, "POST", f"runs/{run['run_id']}/commit", body={"diff_hash": run["diff_hash"]}
    )
    assert result.status_code == 409


async def test_authoritative_entitlement_removal_stops_in_same_transaction(managed):
    await installations.paid(managed.store, revision=2, services=False)
    async with managed.factory() as db:
        row = await db.get(SyncManagedTarget, managed.iid)
        assert row.mode == "stopping" and row.generation == 2
        assert row.stop_reason == "entitlement_ended"
    assert (await finalize(managed))["mode"] == "manual"


async def test_commit_deadline_rollback_never_half_retires_manual_content(managed, monkeypatch):
    await manual(managed)
    async with managed.factory() as db:
        memory = Memory(
            user_id=managed.store.users["owner"],
            workspace_id=managed.store.wid,
            context_id=UUID(managed.target["context_id"]),
            summary="Keep this",
            content="Authored manual body",
            type="note",
            client="pytest",
        )
        await MemoryRepository(db).create(memory)
        mid = memory.id
        await db.commit()
    await pin(managed, [product()])
    p = (await human(managed, "POST", "management/resume/preview")).json()
    original = sync_resume.retire_manual

    async def advance_after_retirement(*args, **kwargs):
        await original(*args, **kwargs)
        managed.store.clock.now += timedelta(minutes=15)

    monkeypatch.setattr(sync_resume, "retire_manual", advance_after_retirement)
    result = await human(
        managed,
        "POST",
        "management/resume/confirm",
        body={
            "operation_id": str(uuid4()),
            "preview_id": p["preview_id"],
            "diff_hash": p["diff_hash"],
        },
    )
    assert result.status_code == 409, result.text
    async with managed.factory() as db:
        assert (await db.get(SyncManagedTarget, managed.iid)).mode == "manual"
        assert (await db.get(SyncInstallation, managed.iid)).generation == 2
        memory = await db.get(Memory, mid)
        assert memory.deleted_at is None and memory.content == "Authored manual body"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(MemoryRevision)
                .where(MemoryRevision.memory_id == mid)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SyncManualRetirement)
                .where(SyncManualRetirement.memory_id == mid)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(DeferredPointDelete)
                .where(DeferredPointDelete.memory_id == mid)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(SyncResumeCandidate).where(
                    SyncResumeCandidate.installation_id == managed.iid
                )
            )
        ).active
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ResourceEvent)
                .where(ResourceEvent.resource_pk == UUID(managed.target["resource_pk"]))
            )
            == 0
        )


async def test_new_candidate_for_each_feed_frees_four_run_limit_and_rejects_incomplete_set(managed):
    await manual(managed)
    await pin(managed, [product()])
    for index in range(5):
        fid = uuid4()
        result = await human(
            managed, "PUT", f"feeds/{fid}", body={"kind": "product", "config_hash": "1" * 64}
        )
        assert result.status_code == 200
        incomplete = await human(managed, "POST", "management/resume/preview")
        assert incomplete.status_code == 409
        result = await staged(managed, [product(str(index))], feed_id=str(fid))
        pinned = await service(
            managed, "POST", "resume-candidates", body={"run_id": result["run_id"]}
        )
        assert pinned.status_code == 200, pinned.text
    result = await human(managed, "POST", "management/resume/preview")
    assert result.status_code == 200, result.text
    assert result.json()["totals"]["publish"] == 6


async def test_equal_commit_times_use_revision_and_noop_run_cannot_bypass_postconditions(
    managed, monkeypatch
):
    await committed(managed, [product(title="first")])
    await committed(managed, [product(title="second")])
    await committed(managed, [product(title="second")])
    stopped = await stop(managed)
    async with managed.factory() as db:
        db.add(
            IndexerState(
                resource_pk=UUID(managed.target["resource_pk"]),
                resource_id=managed.target["resource_id"],
                context_id=UUID(managed.target["context_id"]),
                last_offset=stopped["stop_event_id"],
                metrics={},
            )
        )
        await db.commit()
    refused = await human(
        managed, "POST", "management/finalize", body={"operation_id": str(uuid4())}
    )
    assert refused.status_code == 409
    await apply_pending(managed, monkeypatch)
    assert (await finalize(managed))["mode"] == "manual"


async def test_savepoint_rollback_releases_lock_but_not_cached_authority(managed):
    ctx = UUID(managed.target["context_id"])
    async with managed.factory() as db:
        nested = await db.begin_nested()
        await target_fence(db, ctx)
        await nested.rollback()
        # A different transaction can now finish the exclusive stop.
        assert (await stop(managed))["mode"] == "stopping"
        # The old outer transaction must refresh, rather than reuse a lost shared lock.
        with pytest.raises(ManagedDataLockedError):
            await assert_context_mutable(db, ctx)


async def test_preview_flush_expiry_rolls_back_preview_receipt(managed):
    await manual(managed)
    await pin(managed, [product()])

    def expire_on_flush(session, _context, _instances):
        if any(isinstance(row, SyncResumePreview) for row in session.new):
            managed.store.clock.now += timedelta(minutes=15)

    event.listen(Session, "before_flush", expire_on_flush)
    try:
        response = await human(managed, "POST", "management/resume/preview")
        assert response.status_code == 409, response.text
    finally:
        event.remove(Session, "before_flush", expire_on_flush)
    async with managed.factory() as db:
        assert await db.get(SyncResumePreview, managed.iid) is None


async def test_nonversioned_context_edit_invalidates_manual_consent(managed):
    await manual(managed)
    async with managed.factory() as db:
        row = Memory(
            user_id=managed.store.users["owner"],
            workspace_id=managed.store.wid,
            context_id=UUID(managed.target["context_id"]),
            summary="Manual",
            content="Manual text",
            type="note",
            client="pytest",
            context={"meaning": "before"},
        )
        await MemoryRepository(db).create(row)
        mid, version = row.id, row.version
        await db.commit()
    await pin(managed, [product()])
    p = (await human(managed, "POST", "management/resume/preview")).json()
    async with managed.factory() as db:
        row = await db.get(Memory, mid)
        row.context = {"meaning": "after"}
        await MemoryRepository(db).update(mid, row)
        assert row.version == version
        await db.commit()
    result = await human(
        managed,
        "POST",
        "management/resume/confirm",
        body={
            "operation_id": str(uuid4()),
            "preview_id": p["preview_id"],
            "diff_hash": p["diff_hash"],
        },
    )
    assert result.status_code == 409


async def test_stop_flush_expiry_rolls_back_generation_and_receipt(managed):
    def expire_on_receipt(session, _context, _instances):
        if any(isinstance(row, SyncModeOperation) for row in session.new):
            managed.store.clock.now += timedelta(hours=8)

    event.listen(Session, "before_flush", expire_on_receipt)
    try:
        result = await human(
            managed, "POST", "management/stop", body={"operation_id": str(uuid4())}
        )
        assert result.status_code == 409, result.text
    finally:
        event.remove(Session, "before_flush", expire_on_receipt)
    async with managed.factory() as db:
        assert (await db.get(SyncManagedTarget, managed.iid)).mode == "feed"
        assert (await db.get(SyncInstallation, managed.iid)).generation == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SyncModeOperation)
                .where(SyncModeOperation.installation_id == managed.iid)
            )
            == 0
        )


async def test_repeated_resume_reuses_one_exact_retirement_proof_per_memory(managed, monkeypatch):
    await committed(managed, [product()])
    await apply_pending(managed, monkeypatch)
    for _ in range(3):
        await manual(managed)
        await pin(managed, [product()])
        p = (await human(managed, "POST", "management/resume/preview")).json()
        result = await human(
            managed,
            "POST",
            "management/resume/confirm",
            body={
                "operation_id": str(uuid4()),
                "preview_id": p["preview_id"],
                "diff_hash": p["diff_hash"],
            },
        )
        assert result.status_code == 200, result.text
        await apply_pending(managed, monkeypatch)
        async with managed.factory() as db:
            proofs = (
                await db.scalars(
                    select(SyncManualRetirement).where(
                        SyncManualRetirement.installation_id == managed.iid
                    )
                )
            ).all()
            memories = (
                await db.scalars(
                    select(Memory).where(
                        Memory.context_id == UUID(managed.target["context_id"]),
                        Memory.deleted_at.is_not(None),
                    )
                )
            ).all()
            assert len(proofs) == len(memories)
            assert len({row.memory_id for row in proofs}) == len(proofs)


async def test_maintenance_pages_past_100_blocked_drains_to_expired_target(managed):
    async with managed.factory() as db:
        original = await db.get(SyncInstallation, managed.iid)
        for index in range(1, 102):
            wid, iid = uuid4(), UUID(int=index)
            managed.management_extra_workspaces.append(wid)
            db.add(
                Workspace(
                    id=wid,
                    name=f"Blocked-{index}",
                    owner_user_id=managed.store.users["owner"],
                    plan_name="promax",
                )
            )
            await db.flush()
            installation = SyncInstallation(
                id=iid,
                workspace_id=wid,
                service_id=original.service_id,
                service_client_id=original.service_client_id,
                authorization_id=uuid4(),
                application_id=f"blocked-{index}",
                operations=original.operations,
                context_id=uuid4(),
                resource_pk=uuid4(),
                endpoint_id=uuid4(),
                resource_id=f"blocked-{index}",
                generation=2,
            )
            db.add(installation)
            await db.flush()
            db.add(
                SyncManagedTarget(
                    installation_id=iid,
                    context_id=installation.context_id,
                    resource_pk=installation.resource_pk,
                    mode="stopping",
                    generation=2,
                    drain_generation=1,
                    stop_event_id=1,
                    event_floor=0,
                    epoch=2,
                    manual_sleep_mode="skip",
                )
            )
        await db.commit()
    managed.store.clock.now += timedelta(days=15)
    for _ in range(2):
        async with managed.factory() as db:
            await sync_management.maintain_sync_management(db)
    async with managed.factory() as db:
        assert (await db.get(SyncManagedTarget, managed.iid)).mode == "manual"
        assert (await db.get(SyncManagedTarget, UUID(int=1))).mode == "stopping"


@pytest.mark.parametrize("revoked", [False, True])
async def test_legacy_pending_source_is_held_then_drained_at_its_real_generation(
    feeds, monkeypatch, revoked
):
    await committed(feeds, [product()])  # pre-barrier accepted source; no ownership row
    if revoked:
        result = await installations.http(feeds.store, "DELETE", f"sync-installations/{feeds.iid}")
        assert result.status_code == 200
    monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", True)
    qdrant = MagicMock()
    qdrant.upsert, qdrant.delete = AsyncMock(), AsyncMock()
    embedding = AsyncMock()
    embedding.embed.return_value = [0.1] * 1536
    monkeypatch.setattr(resource_indexer, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(
        resource_indexer,
        "resolve_context_routing",
        AsyncMock(return_value=("test-collection", embedding)),
    )
    monkeypatch.setattr(
        resource_indexer.QuotaService,
        "check_memories_per_day",
        AsyncMock(return_value=(True, None)),
    )

    async def process(db):
        indexer = resource_indexer.ResourceIndexer(db)
        indexer._plan_suspended = AsyncMock(return_value=False)
        indexer._workspace_memory_room = AsyncMock(return_value=100)
        return await indexer.process_incremental(
            feeds.target["resource_id"], UUID(feeds.target["context_id"])
        )

    async with feeds.factory() as db:
        assert (await process(db)).errors == 1
        state = await db.scalar(
            select(IndexerState).where(IndexerState.context_id == UUID(feeds.target["context_id"]))
        )
        assert state is None or state.last_offset == 0
    async with feeds.factory() as db:
        from services.workspace_locks import lock_workspace_for_update

        await lock_workspace_for_update(db, feeds.store.wid)
        installation = await db.get(SyncInstallation, feeds.iid)
        await sync_management.start_stopping(db, installation, "manual_request")
        await db.commit()
    async with feeds.factory() as db:
        metrics = await process(db)
        assert metrics.errors == 0 and metrics.batch_committed, metrics.to_dict()
    async with feeds.factory() as db:
        from services.workspace_locks import lock_workspace_for_update

        await lock_workspace_for_update(db, feeds.store.wid)
        installation = await db.get(SyncInstallation, feeds.iid)
        target = await sync_management.finish_stopping(db, installation)
        assert target.mode == "manual"
        await db.commit()
    assert qdrant.upsert.await_count == 1


async def test_physical_target_loss_retains_fence_and_admin_recovery_cannot_bypass(
    managed, monkeypatch
):
    from api.routes.admin import ContextRecoveryRequest, recover_context

    async with managed.factory() as db:
        await db.delete(await db.get(Context, UUID(managed.target["context_id"])))
        await db.commit()
    monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", False)
    async with managed.factory() as db:
        assert await db.get(SyncManagedTarget, managed.iid) is not None
        with pytest.raises(ManagedDataLockedError):
            await recover_context(
                ContextRecoveryRequest(context_id=managed.target["context_id"], dry_run=False),
                user={"user_id": managed.store.users["owner"]},
                db=db,
            )


async def test_move_out_and_generic_embedding_rewrite_remain_locked(managed, monkeypatch):
    from services.vector_erasure import embedding_author_current

    await committed(managed, [product()])
    await apply_pending(managed, monkeypatch)
    async with managed.factory() as db:
        memory = await db.scalar(
            select(Memory).where(Memory.context_id == UUID(managed.target["context_id"]))
        )
        with pytest.raises(ManagedDataLockedError):
            await embedding_author_current(db, memory)
        memory.context_id = uuid4()
        with pytest.raises(ManagedDataLockedError):
            await MemoryRepository(db).update(memory.id, memory)


async def test_actual_batch_suppresses_old_source_in_manual_and_honors_resume_capacity(
    managed, monkeypatch
):
    await committed(managed, [product()])
    await apply_pending(managed, monkeypatch)
    await manual(managed)
    async with managed.factory() as db:
        memory = await db.scalar(
            select(Memory).where(Memory.context_id == UUID(managed.target["context_id"]))
        )
        await MemoryRepository(db).soft_delete(memory.id, deleted_by=managed.store.users["owner"])
        manual_row = Memory(
            user_id=managed.store.users["owner"],
            workspace_id=managed.store.wid,
            context_id=UUID(managed.target["context_id"]),
            summary="My manual note",
            content="Keep until confirmed",
            type="note",
            client="pytest",
        )
        await MemoryRepository(db).create(manual_row)
        mid = manual_row.id
        state = await db.scalar(
            select(IndexerState).where(
                IndexerState.context_id == UUID(managed.target["context_id"])
            )
        )
        state.last_offset = 0
        await db.commit()
    qdrant, embedding = MagicMock(), AsyncMock()
    qdrant.upsert, qdrant.delete = AsyncMock(), AsyncMock()
    embedding.embed.return_value = [0.1] * 1536
    quota = AsyncMock(return_value=(False, "No daily room"))
    monkeypatch.setattr(resource_indexer, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(
        resource_indexer,
        "resolve_context_routing",
        AsyncMock(return_value=("test-collection", embedding)),
    )
    monkeypatch.setattr(resource_indexer.QuotaService, "check_memories_per_day", quota)

    async def process(room):
        async with managed.factory() as db:
            indexer = resource_indexer.ResourceIndexer(db)
            indexer._plan_suspended = AsyncMock(return_value=False)
            indexer._workspace_memory_room = AsyncMock(return_value=room)
            return await indexer.process_incremental(
                managed.target["resource_id"], UUID(managed.target["context_id"])
            )

    result = await process(0)
    assert result.batch_committed and result.errors == 0
    quota.assert_not_awaited()
    qdrant.upsert.assert_not_awaited()
    async with managed.factory() as db:
        assert (await db.get(Memory, mid)).content == "Keep until confirmed"
    await pin(managed, [product(title="New generation")])
    p = (await human(managed, "POST", "management/resume/preview")).json()
    response = await human(
        managed,
        "POST",
        "management/resume/confirm",
        body={
            "operation_id": str(uuid4()),
            "preview_id": p["preview_id"],
            "diff_hash": p["diff_hash"],
        },
    )
    assert response.status_code == 200, response.text
    result = await process(0)
    assert result.skipped and result.reason == "memory_limit_exceeded"
    qdrant.upsert.assert_not_awaited()
    result = await process(100)
    assert result.skipped and result.reason == "memories_per_day_exceeded"
    qdrant.upsert.assert_not_awaited()
    quota.return_value = (True, None)
    result = await process(100)
    assert result.batch_committed and result.errors == 0, result.to_dict()
    assert qdrant.upsert.await_count == 1
    await stop(managed)
    assert (await finalize(managed))["mode"] == "manual"


async def test_diff_page_must_still_be_live_after_snapshot_work(managed, monkeypatch):
    await manual(managed)
    await pin(managed, [product()])
    p = (await human(managed, "POST", "management/resume/preview")).json()
    original = sync_resume.manual_snapshot

    async def slow_snapshot(*args, **kwargs):
        result = await original(*args, **kwargs)
        managed.store.clock.now += timedelta(minutes=15)
        return result

    monkeypatch.setattr(sync_resume, "manual_snapshot", slow_snapshot)
    result = await human(
        managed, "GET", "management/resume/diff", params={"preview_id": p["preview_id"]}
    )
    assert result.status_code == 409


async def test_realistically_ticking_clock_keeps_candidate_and_preview_within_ttl(
    managed, monkeypatch
):
    await manual(managed)
    instant = managed.store.clock.now

    def ticking():
        nonlocal instant
        instant += timedelta(microseconds=1)
        return instant

    monkeypatch.setattr(sync_resume, "utcnow", ticking)
    await pin(managed, [product()])
    result = await human(managed, "POST", "management/resume/preview")
    assert result.status_code == 200, result.text
    async with managed.factory() as db:
        candidate = await db.scalar(
            select(SyncResumeCandidate).where(SyncResumeCandidate.installation_id == managed.iid)
        )
        preview = await db.get(SyncResumePreview, managed.iid)
        assert candidate.expires_at - candidate.created_at == timedelta(hours=24)
        assert preview.expires_at - preview.created_at == timedelta(minutes=15)


async def test_legacy_ordinary_backlog_is_not_silently_acknowledged_or_enrolled(feeds, monkeypatch):
    async with feeds.factory() as db:
        db.add(
            ResourceEvent(
                resource_pk=UUID(feeds.target["resource_pk"]),
                resource_id=feeds.target["resource_id"],
                op="upsert",
                doc_id="legacy-ordinary",
                version=1,
                payload={"title": "Ordinary accepted content"},
            )
        )
        await db.commit()
    monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", True)
    async with feeds.factory() as db:
        installation = await db.get(SyncInstallation, feeds.iid)
        with pytest.raises(ConflictError, match="Drain ordinary ingestion"):
            await sync_management.enroll(db, installation)
        await db.rollback()
    async with feeds.factory() as db:
        assert await db.get(SyncManagedTarget, feeds.iid) is None


@pytest.mark.parametrize("entrypoint", ["service", "legacy_task"])
async def test_working_retention_skips_managed_data_and_cleans_ordinary_context(
    managed, monkeypatch, entrypoint
):
    from services.memory_service import MemoryService
    from utils.datetime import utcnow

    await committed(managed, [product()])
    await apply_pending(managed, monkeypatch)
    async with managed.factory() as db:
        source = await db.scalar(
            select(Memory).where(Memory.context_id == UUID(managed.target["context_id"]))
        )
        source.created_at = utcnow() - timedelta(days=90)
        source_id = source.id
        context = Context(
            id=uuid4(),
            workspace_id=managed.store.wid,
            name="ordinary-retention",
            created_by=managed.store.users["owner"],
            is_private=False,
        )
        db.add(context)
        await db.flush()
        ordinary = Memory(
            user_id=managed.store.users["owner"],
            workspace_id=managed.store.wid,
            context_id=context.id,
            summary="Ordinary old note",
            content="Ordinary",
            type="note",
            client="pytest",
            scope="working",
            importance=0.5,
            created_at=utcnow() - timedelta(days=90),
        )
        await MemoryRepository(db).create(ordinary)
        ordinary_id = ordinary.id
        if entrypoint == "legacy_task":
            db.add(GraphMemory(user_id=managed.store.users["owner"], graph_data={}))
        await db.commit()
        if entrypoint == "service":
            service_instance = MemoryService(db)
            service_instance._queue_forgotten_points = AsyncMock(return_value=[])
            service_instance.delete_forgotten_points = AsyncMock()
            assert (
                await service_instance.cleanup_old_working_memories(managed.store.users["owner"])
                == 1
            )
        else:
            from tasks import neural_tasks

            async def session_source():
                yield db

            monkeypatch.setattr(neural_tasks, "get_db", session_source)
            monkeypatch.setattr(
                MemoryService, "_queue_forgotten_points", AsyncMock(return_value=[])
            )
            monkeypatch.setattr(MemoryService, "delete_forgotten_points", AsyncMock())
            monkeypatch.setenv("SLEEP_ENABLED", "false")
            try:
                result = await neural_tasks.consolidation_task()
                assert result["deleted"] >= 1, result
            finally:
                await db.rollback()
                await db.execute(
                    delete(GraphMemory).where(GraphMemory.user_id == managed.store.users["owner"])
                )
                await db.commit()
    async with managed.factory() as db:
        assert await db.get(Memory, source_id) is not None
        assert await db.get(Memory, ordinary_id) is None
