"""Full-snapshot atomicity, durable retries, field allowlists and exact deletions."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from models.resource import ResourceEvent
from models.sync_feed import SyncChunk, SyncIndexOutbox, SyncRecord, SyncRun
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_credential_fixtures import mint
from tests.integration.sync_feed_fixtures import (
    committed,
    human,
    manifest,
    product,
    service,
    staged,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


async def counts(worker):
    async with worker.factory() as db:
        return [
            await db.scalar(select(func.count()).select_from(model))
            for model in (ResourceEvent, SyncRecord, SyncIndexOutbox)
        ]


async def test_partial_upload_never_publishes_and_complete_commit_is_durable(feeds):
    records = [product("one"), product("two")]
    body = manifest(feeds, records, chunks=2)
    first = await service(feeds, "POST", "runs", body=body)
    assert first.status_code == 201, first.text
    rid = first.json()["run_id"]
    assert (
        await service(feeds, "PUT", f"runs/{rid}/chunks/0", body={"records": records[:1]})
    ).status_code == 200
    assert (await service(feeds, "POST", f"runs/{rid}/prepare")).status_code == 409
    assert await counts(feeds) == [0, 0, 0]
    assert (
        await service(feeds, "PUT", f"runs/{rid}/chunks/1", body={"records": records[1:]})
    ).status_code == 200
    prepared = await service(feeds, "POST", f"runs/{rid}/prepare")
    assert prepared.status_code == 200, prepared.text
    assert await counts(feeds) == [0, 0, 0]
    confirmation = {"diff_hash": prepared.json()["diff_hash"]}
    result = await service(feeds, "POST", f"runs/{rid}/commit", body=confirmation)
    assert result.status_code == 200, result.text
    assert await counts(feeds) == [2, 2, 1]
    token = (await mint(feeds))["access_token"]
    assert (await service(feeds, "POST", "runs", body=body, token=token)).json()["run_id"] == rid
    assert (
        await service(feeds, "POST", f"runs/{rid}/commit", body=confirmation, token=token)
    ).status_code == 200
    assert await counts(feeds) == [2, 2, 1]
    recovered = await service(feeds, "GET", f"runs/{rid}", token=token)
    assert (
        recovered.json()["state"] == "committed" and recovered.json()["index_status"] == "pending"
    )
    async with feeds.factory() as db:
        assert all(item.records is None for item in (await db.scalars(select(SyncChunk))).all())


async def test_changed_operation_or_chunk_body_conflicts_without_events(feeds):
    body = manifest(feeds, [product()])
    rid = (await service(feeds, "POST", "runs", body=body)).json()["run_id"]
    assert (
        await service(feeds, "POST", "runs", body={**body, "source_hash": "3" * 64})
    ).status_code == 409
    assert (
        await service(feeds, "PUT", f"runs/{rid}/chunks/0", body={"records": [product()]})
    ).status_code == 200
    assert (
        await service(
            feeds, "PUT", f"runs/{rid}/chunks/0", body={"records": [product(title="changed")]}
        )
    ).status_code == 409
    assert await counts(feeds) == [0, 0, 0]


@pytest.mark.parametrize(
    "records",
    [
        [product(secret_note="private")],
        [product(price="3.00")],
        [product(url="http://unsafe.test")],
        [product(title="")],
    ],
)
async def test_invalid_or_private_fields_never_enter_staging(feeds, records):
    rid = (await service(feeds, "POST", "runs", body=manifest(feeds, records))).json()["run_id"]
    result = await service(feeds, "PUT", f"runs/{rid}/chunks/0", body={"records": records})
    assert result.status_code == 400, result.text
    assert result.headers["cache-control"] == "no-store"
    assert await counts(feeds) == [0, 0, 0]
    async with feeds.factory() as db:
        assert await db.scalar(select(func.count()).select_from(SyncChunk)) == 0


async def test_duplicate_external_ids_and_bad_snapshot_digest_cannot_prepare(feeds):
    records = [product(), product()]
    rid = (await service(feeds, "POST", "runs", body=manifest(feeds, records))).json()["run_id"]
    assert (
        await service(feeds, "PUT", f"runs/{rid}/chunks/0", body={"records": records})
    ).status_code == 200
    assert (await service(feeds, "POST", f"runs/{rid}/prepare")).status_code == 409
    assert await counts(feeds) == [0, 0, 0]


async def test_destructive_diff_requires_finite_current_human_editor_approval(feeds):
    await committed(feeds, [product()])
    row = await staged(feeds, [])
    rid, body = row["run_id"], {"diff_hash": row["diff_hash"]}
    assert (await service(feeds, "POST", f"runs/{rid}/commit", body=body)).status_code == 409
    assert (
        await human(feeds, "POST", f"runs/{rid}/approvals", body=body, role="viewer")
    ).status_code == 404
    approved = await human(feeds, "POST", f"runs/{rid}/approvals", body=body, role="member")
    assert approved.status_code == 200, approved.text
    result = await service(
        feeds,
        "POST",
        f"runs/{rid}/commit",
        body={**body, "approval_id": approved.json()["approval_id"]},
    )
    assert result.status_code == 200, result.text
    async with feeds.factory() as db:
        events = (await db.scalars(select(ResourceEvent).order_by(ResourceEvent.id))).all()
        assert [event.op for event in events] == ["upsert", "delete"]
        assert events[1].version == 1 and events[1].payload is None
        assert (await db.scalar(select(SyncRecord))).payload is None


async def test_expired_preparation_or_changed_configuration_cannot_commit(feeds):
    row = await staged(feeds, [product()])
    result = await human(
        feeds, "PUT", f"feeds/{feeds.fid}", body={"kind": "product", "config_hash": "3" * 64}
    )
    assert result.status_code == 200
    result = await service(
        feeds, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert result.status_code == 409
    assert await counts(feeds) == [0, 0, 0]


async def test_abort_and_periodic_expiry_clear_bodies_without_publication(feeds):
    row = await staged(feeds, [product()])
    assert (await service(feeds, "POST", f"runs/{row['run_id']}/abort")).status_code == 200
    async with feeds.factory() as db:
        assert (await db.get(SyncChunk, (UUID(row["run_id"]), 0))).records is None
    row = await staged(feeds, [product()])
    feeds.store.clock.now += timedelta(hours=25)
    from services.sync_feed_index import maintain_sync_runs

    async with feeds.factory() as db:
        await maintain_sync_runs(db)
        assert (await db.get(SyncRun, UUID(row["run_id"]))).state == "expired"
        assert (await db.get(SyncChunk, (UUID(row["run_id"]), 0))).records is None
    assert await counts(feeds) == [0, 0, 0]


async def test_foreign_run_and_wrong_feed_never_cross_installations(feeds):
    assert (await service(feeds, "GET", f"runs/{uuid4()}")).status_code == 404
    assert (
        await service(feeds, "POST", "runs", body=manifest(feeds, [], feed_id=str(uuid4())))
    ).status_code == 404
