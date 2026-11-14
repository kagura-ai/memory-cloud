"""Real SQL index handoff, crash recovery, semantic text and ownership barriers."""

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.sql import Select

from models.auth import Context
from models.memory import Memory
from models.resource import IndexerState, ResourceEvent, ResourceSchema
from models.sync_feed import SyncIndexOutbox, SyncRecord, SyncRun
from services import resource_indexer
from services.sync_feed_index import hold_sync_index_receipt, maintain_sync_runs
from services.sync_feed_provenance import guard_sync_event
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_feed_fixtures import committed, human, product, service, staged
from utils.exceptions import ConflictError, QdrantError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


async def test_actual_indexer_embeds_product_text_and_publishes_owned_memory(feeds, monkeypatch):
    row = await committed(feeds, [product(description="Searchable approved description")])
    embed = AsyncMock()
    embed.embed.return_value = [0.1] * 1536
    qdrant = MagicMock()
    qdrant.upsert = AsyncMock()
    monkeypatch.setattr(resource_indexer, "get_qdrant_client", lambda: qdrant)
    async with feeds.factory() as db:
        event = await db.scalar(select(ResourceEvent))
        context = await db.get(Context, UUID(feeds.target["context_id"]))
        schema = await db.scalar(select(ResourceSchema))
        indexer = resource_indexer.ResourceIndexer(db)
        assert await guard_sync_event(db, event, context)
        await indexer._apply_upsert(event, schema, context, "test-collection", embed)
        await db.commit()
        memory = await db.scalar(
            select(Memory).where(Memory.context_id == context.id, Memory.deleted_at.is_(None))
        )
        assert memory is not None and json.loads(memory.content)["title"] == "Public product one"
        assert "Public product one" in embed.embed.call_args.kwargs["text"]
        assert "Searchable approved description" in embed.embed.call_args.kwargs["text"]
        assert qdrant.upsert.await_count == 1
    async with feeds.factory() as db:
        await maintain_sync_runs(db)
        state = await db.scalar(select(IndexerState))
        state.last_offset = (await db.get(SyncIndexOutbox, UUID(row["run_id"]))).last_event_id
        await db.commit()
    assert (await service(feeds, "GET", f"runs/{row['run_id']}")).json()[
        "index_status"
    ] == "complete"


async def test_obsolete_pending_upserts_cannot_reappear_after_canonical_delete(feeds):
    await committed(feeds, [product()])
    await committed(feeds, [product(title="newer")])
    row = await staged(feeds, [])
    approval = (
        await human(
            feeds, "POST", f"runs/{row['run_id']}/approvals", body={"diff_hash": row["diff_hash"]}
        )
    ).json()
    result = await service(
        feeds,
        "POST",
        f"runs/{row['run_id']}/commit",
        body={"diff_hash": row["diff_hash"], "approval_id": approval["approval_id"]},
    )
    assert result.status_code == 200, result.text
    async with feeds.factory() as db:
        context = await db.get(Context, UUID(feeds.target["context_id"]))
        old_events = (
            await db.scalars(select(ResourceEvent).where(ResourceEvent.op == "upsert"))
        ).all()
        assert len(old_events) == 2
        for event in old_events:
            assert await guard_sync_event(db, event, context) is False


async def test_outbox_recovers_crash_after_running_but_does_not_steal_live_batch(feeds):
    await committed(feeds, [product()])
    async with feeds.factory() as db:
        await maintain_sync_runs(db)
        state = await db.scalar(select(IndexerState))
        state.job_status, state.last_run_at = "running", feeds.store.clock.now - timedelta(hours=1)
        await db.commit()
    async with feeds.factory() as active:
        await hold_sync_index_receipt(
            active, UUID(feeds.target["resource_pk"]), UUID(feeds.target["context_id"])
        )
        async with feeds.factory() as db:
            await maintain_sync_runs(db)
            assert (await db.scalar(select(IndexerState))).job_status == "running"
        await active.rollback()  # A process crash releases this transaction's receipt lock.
    async with feeds.factory() as db:
        await maintain_sync_runs(db)
        assert (await db.scalar(select(IndexerState))).job_status == "queued"


async def test_older_held_receipts_do_not_starve_a_different_healthy_target(feeds):
    first = await committed(feeds, [product()])
    async with feeds.factory() as db:
        await maintain_sync_runs(db)
        original = await db.get(SyncRun, UUID(first["run_id"]))
        outbox = await db.get(SyncIndexOutbox, original.id)
        for _index in range(101):
            run = SyncRun(
                feed_id=original.feed_id,
                operation_id=uuid4(),
                request_hash="1" * 64,
                manifest={},
                generations=[1, 1, 1],
                base_revision=0,
                state="committed",
                expires_at=feeds.store.clock.now + timedelta(days=1),
            )
            db.add(run)
            await db.flush()
            db.add(
                SyncIndexOutbox(
                    run_id=run.id,
                    resource_pk=outbox.resource_pk,
                    context_id=outbox.context_id,
                    last_event_id=outbox.last_event_id,
                    event_ids=[],
                )
            )
        # A second resource/context is enough to exercise target fairness independently
        # of an installation's provisioning and payment (covered by KMC-04/05).
        from models.resource import Resource

        resource = Resource(workspace_id=feeds.store.wid, resource_id="fairness-" + uuid4().hex)
        db.add(resource)
        await db.flush()
        context = Context(
            workspace_id=feeds.store.wid,
            name=resource.resource_id,
            resource_id=resource.resource_id,
            is_public=True,
            is_private=False,
        )
        db.add(context)
        await db.flush()
        run = SyncRun(
            feed_id=original.feed_id,
            operation_id=uuid4(),
            request_hash="2" * 64,
            manifest={},
            generations=[1, 1, 1],
            base_revision=0,
            state="committed",
            expires_at=feeds.store.clock.now + timedelta(days=1),
        )
        db.add(run)
        await db.flush()
        db.add(
            SyncIndexOutbox(
                run_id=run.id,
                resource_pk=resource.id,
                context_id=context.id,
                last_event_id=outbox.last_event_id + 1,
                event_ids=[],
            )
        )
        await db.commit()
        target = resource.id
    async with feeds.factory() as db:
        await maintain_sync_runs(db)
        state = await db.scalar(select(IndexerState).where(IndexerState.resource_pk == target))
        assert state is not None and state.job_status == "queued"


async def test_failed_older_version_delete_never_reports_search_complete(feeds):
    await committed(feeds, [product()])
    async with feeds.factory() as db:
        record = await db.scalar(select(SyncRecord))
        context = await db.get(Context, UUID(feeds.target["context_id"]))
        db.add(
            Memory(
                user_id=feeds.store.users["owner"],
                workspace_id=feeds.store.wid,
                context_id=context.id,
                summary="Old indexed version",
                content=json.dumps(record.payload),
                details={
                    "resource_id": feeds.target["resource_id"],
                    "doc_id": record.doc_id,
                    "version": 1,
                },
                source="resource_ingest",
                client="resource_indexer",
                source_type="connector",
                version=1,
                embedding_status="success",
                type="fact",
                scope="working",
                importance=0.6,
            )
        )
        await db.commit()
    await committed(feeds, [product(title="version two, indexing fails")])
    row = await staged(feeds, [])
    approval = (
        await human(
            feeds, "POST", f"runs/{row['run_id']}/approvals", body={"diff_hash": row["diff_hash"]}
        )
    ).json()
    result = await service(
        feeds,
        "POST",
        f"runs/{row['run_id']}/commit",
        body={"diff_hash": row["diff_hash"], "approval_id": approval["approval_id"]},
    )
    assert result.status_code == 200, result.text
    async with feeds.factory() as db:
        await maintain_sync_runs(db)
        state = await db.scalar(select(IndexerState))
        state.last_offset = (await db.get(SyncIndexOutbox, UUID(row["run_id"]))).last_event_id
        await db.commit()
    assert (await service(feeds, "GET", f"runs/{row['run_id']}")).json()["index_status"] == "failed"


async def test_full_process_skips_alternating_obsolete_upserts_before_zero_room_and_quota(
    feeds, monkeypatch
):
    for title in ("First lifetime", "Second lifetime"):
        await committed(feeds, [product(title=title)])
        row = await staged(feeds, [])
        body = {"diff_hash": row["diff_hash"]}
        approval = (await human(feeds, "POST", f"runs/{row['run_id']}/approvals", body=body)).json()
        assert (
            await service(
                feeds,
                "POST",
                f"runs/{row['run_id']}/commit",
                body={**body, "approval_id": approval["approval_id"]},
            )
        ).status_code == 200
    qdrant = MagicMock()
    qdrant.upsert, qdrant.delete = AsyncMock(), AsyncMock()
    embed = AsyncMock()
    quota = AsyncMock(return_value=(False, "no daily room"))
    monkeypatch.setattr(resource_indexer, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(
        resource_indexer,
        "resolve_context_routing",
        AsyncMock(return_value=("test-collection", embed)),
    )
    monkeypatch.setattr(resource_indexer.QuotaService, "check_memories_per_day", quota)
    async with feeds.factory() as db:
        indexer = resource_indexer.ResourceIndexer(db)
        indexer._plan_suspended = AsyncMock(return_value=False)
        indexer._workspace_memory_room = AsyncMock(return_value=0)
        metrics = await indexer.process_incremental(
            feeds.target["resource_id"], UUID(feeds.target["context_id"])
        )
        assert metrics.batch_committed and not metrics.reason, metrics.to_dict()
        assert quota.await_count == 0 and embed.embed.await_count == 0
        assert qdrant.upsert.await_count == 0
        assert (await db.scalar(select(IndexerState))).last_offset > 0
    assert (await service(feeds, "GET", f"runs/{row['run_id']}")).json()[
        "index_status"
    ] == "complete"


async def test_foreign_tombstone_cannot_be_retired_by_sync_upsert(feeds):
    await committed(feeds, [product()])
    async with feeds.factory() as db:
        record = await db.scalar(select(SyncRecord))
        memory = Memory(
            user_id=feeds.store.users["member"],
            workspace_id=feeds.store.wid,
            context_id=UUID(feeds.target["context_id"]),
            summary="Foreign deleted row",
            content="Foreign deleted body",
            details={
                "resource_id": feeds.target["resource_id"],
                "doc_id": record.doc_id,
                "version": 55,
            },
            source="mcp_remember",
            client="test",
            source_type="api",
            version=1,
            deleted_at=feeds.store.clock.now,
            type="fact",
            scope="working",
            importance=0.6,
        )
        db.add(memory)
        await db.commit()
        memory_id = memory.id
    row = await staged(feeds, [product(title="new value")])
    result = await service(
        feeds, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert result.status_code == 409
    async with feeds.factory() as db:
        context = await db.get(Context, UUID(feeds.target["context_id"]))
        event = await db.scalar(select(ResourceEvent).where(ResourceEvent.op == "upsert"))
        with pytest.raises(ConflictError):
            await guard_sync_event(db, event, context)
        assert (await db.get(Memory, memory_id)).content == "Foreign deleted body"


@pytest.mark.parametrize("collision", ["current", "retired", "delete"])
async def test_late_details_move_never_overwrites_or_deletes_foreign_memory(
    feeds, monkeypatch, collision
):
    row = await committed(feeds, [product()])
    if collision == "delete":
        row = await staged(feeds, [])
        body = {"diff_hash": row["diff_hash"]}
        approval = (await human(feeds, "POST", f"runs/{row['run_id']}/approvals", body=body)).json()
        assert (
            await service(
                feeds,
                "POST",
                f"runs/{row['run_id']}/commit",
                body={**body, "approval_id": approval["approval_id"]},
            )
        ).status_code == 200
    async with feeds.factory() as db:
        record = await db.scalar(select(SyncRecord))
        foreign = Memory(
            user_id=feeds.store.users["member"],
            workspace_id=feeds.store.wid,
            context_id=UUID(feeds.target["context_id"]),
            summary="Independent row",
            content="Must remain independent",
            details={"note": "initially not a resource document"},
            source="mcp_remember",
            client="test",
            source_type="api",
            type="fact",
            scope="working",
            importance=0.6,
        )
        db.add(foreign)
        await db.commit()
        foreign_id, doc_id = foreign.id, record.doc_id

    async def move_details():
        async with feeds.factory() as writer:
            foreign = await writer.get(Memory, foreign_id)
            foreign.details = {
                "resource_id": feeds.target["resource_id"],
                "doc_id": doc_id,
                "version": 55 if collision == "retired" else 1,
            }
            # This is the same-column SQL write used by normal Memory PATCH.
            # An unchanged context_id does not take a fresh FK Context lock.
            await writer.commit()

    qdrant = MagicMock()
    qdrant.upsert, qdrant.delete = AsyncMock(), AsyncMock()
    monkeypatch.setattr(resource_indexer, "get_qdrant_client", lambda: qdrant)
    async with feeds.factory() as db:
        event = await db.scalar(
            select(ResourceEvent).where(
                ResourceEvent.op == ("delete" if collision == "delete" else "upsert")
            )
        )
        context = await db.get(Context, UUID(feeds.target["context_id"]))
        schema = await db.scalar(select(ResourceSchema))
        indexer = resource_indexer.ResourceIndexer(db)
        assert await guard_sync_event(db, event, context)
        if collision == "delete":
            execute = db.execute
            moved = False

            async def move_before_candidate(statement, *args, **kwargs):
                nonlocal moved
                if (
                    not moved
                    and isinstance(statement, Select)
                    and statement.column_descriptions[0].get("entity") is Memory
                ):
                    moved = True
                    await asyncio.wait_for(move_details(), 5)
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(db, "execute", move_before_candidate)
            with pytest.raises(QdrantError) as denied:
                await indexer._apply_delete(event, context, "test-collection")
            assert isinstance(denied.value.__cause__, ConflictError)
        else:
            embed = AsyncMock()

            async def move_during_embedding(**_kwargs):
                await asyncio.wait_for(move_details(), 5)
                return [0.1] * 1536

            embed.embed.side_effect = move_during_embedding
            if collision == "current":
                with pytest.raises(ConflictError):
                    await indexer._apply_upsert(event, schema, context, "test-collection", embed)
                assert qdrant.upsert.await_count == 0
            else:
                await indexer._apply_upsert(event, schema, context, "test-collection", embed)
                await db.commit()
    async with feeds.factory() as db:
        foreign = await db.get(Memory, foreign_id)
        assert foreign is not None and foreign.content == "Must remain independent"
        assert foreign.source == "mcp_remember" and foreign.user_id == feeds.store.users["member"]


async def test_full_replacement_frees_owned_capacity_before_creating_new_ids(feeds, monkeypatch):
    first = await committed(feeds, [product(f"old-{index}") for index in range(3)])
    async with feeds.factory() as db:
        for record in (await db.scalars(select(SyncRecord))).all():
            db.add(
                Memory(
                    user_id=feeds.store.users["owner"],
                    workspace_id=feeds.store.wid,
                    context_id=UUID(feeds.target["context_id"]),
                    summary="Old owned product",
                    content=json.dumps(record.payload),
                    details={
                        "resource_id": feeds.target["resource_id"],
                        "doc_id": record.doc_id,
                        "version": record.version,
                    },
                    source="resource_ingest",
                    client="resource_indexer",
                    source_type="connector",
                    embedding_status="success",
                    type="fact",
                    scope="working",
                    importance=0.6,
                )
            )
        await db.commit()
        await maintain_sync_runs(db)
        state = await db.scalar(select(IndexerState))
        state.last_offset = (await db.get(SyncIndexOutbox, UUID(first["run_id"]))).last_event_id
        previous_offset = state.last_offset
        await db.commit()
    row = await staged(feeds, [product(f"new-{index}") for index in range(3)])
    body = {"diff_hash": row["diff_hash"]}
    approval = (await human(feeds, "POST", f"runs/{row['run_id']}/approvals", body=body)).json()
    assert (
        await service(
            feeds,
            "POST",
            f"runs/{row['run_id']}/commit",
            body={**body, "approval_id": approval["approval_id"]},
        )
    ).status_code == 200
    async with feeds.factory() as db:
        events = (
            await db.scalars(
                select(ResourceEvent)
                .where(ResourceEvent.id > previous_offset)
                .order_by(ResourceEvent.id)
            )
        ).all()
        assert [event.op for event in events] == ["delete"] * 3 + ["upsert"] * 3
    qdrant = MagicMock()
    qdrant.upsert, qdrant.delete = AsyncMock(), AsyncMock()
    embed = AsyncMock()
    embed.embed.return_value = [0.1] * 1536
    monkeypatch.setattr(resource_indexer, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(
        resource_indexer,
        "resolve_context_routing",
        AsyncMock(return_value=("test-collection", embed)),
    )
    monkeypatch.setattr(
        resource_indexer.QuotaService,
        "check_memories_per_day",
        AsyncMock(return_value=(True, None)),
    )
    async with feeds.factory() as db:

        async def room(_workspace_id):
            count = await db.scalar(
                select(func.count())
                .select_from(Memory)
                .where(Memory.workspace_id == feeds.store.wid, Memory.deleted_at.is_(None))
            )
            return 3 - count

        indexer = resource_indexer.ResourceIndexer(db)
        indexer._plan_suspended = AsyncMock(return_value=False)
        indexer._workspace_memory_room = room
        first_batch = await indexer.process_incremental(
            feeds.target["resource_id"], UUID(feeds.target["context_id"])
        )
        assert first_batch.batch_committed and first_batch.applied_deletes == 3
        second_batch = await indexer.process_incremental(
            feeds.target["resource_id"], UUID(feeds.target["context_id"])
        )
        assert second_batch.batch_committed and second_batch.applied_upserts == 3
    assert (await service(feeds, "GET", f"runs/{row['run_id']}")).json()[
        "index_status"
    ] == "complete"
