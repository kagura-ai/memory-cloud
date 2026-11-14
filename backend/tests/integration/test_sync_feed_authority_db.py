"""Rollback, current authority, concurrent baselines and server-owned deletion bounds."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from models.auth import WorkspaceMember
from models.resource import ResourceEvent
from models.sync_credential import SyncCredentialClient
from models.sync_feed import SyncChunk, SyncIndexOutbox, SyncRecord, SyncRun
from models.sync_installation import SyncInstallation
from services import sync_feed_commit
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
from tests.integration.sync_installation_fixtures import change, paid

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


async def test_failure_after_event_flush_rolls_back_canonical_events_and_outbox(feeds, monkeypatch):
    row = await staged(feeds, [product()])

    async def fail(_db, _row):
        raise RuntimeError("simulated failure after resource events were flushed")

    monkeypatch.setattr(sync_feed_commit, "clear_staging", fail)
    result = await service(
        feeds, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert result.status_code == 503
    async with feeds.factory() as db:
        for model in (ResourceEvent, SyncRecord, SyncIndexOutbox):
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.resource_pk == UUID(feeds.target["resource_pk"]))
                )
                == 0
            )
        assert (await db.get(SyncRun, UUID(row["run_id"]))).state == "prepared"
        assert (await db.get(SyncChunk, (UUID(row["run_id"]), 0))).records is not None


async def test_expiry_after_last_await_is_rechecked_by_actual_commit(feeds, monkeypatch):
    row = await staged(feeds, [product()])
    original = sync_feed_commit.clear_staging

    async def expire(db, record):
        await original(db, record)
        feeds.store.clock.now += timedelta(seconds=300)

    monkeypatch.setattr(sync_feed_commit, "clear_staging", expire)
    result = await service(
        feeds, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert result.status_code == 401, result.text
    async with feeds.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ResourceEvent)
                .where(ResourceEvent.resource_pk == UUID(feeds.target["resource_pk"]))
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SyncRecord)
                .where(SyncRecord.resource_pk == UUID(feeds.target["resource_pk"]))
            )
            == 0
        )


@pytest.mark.parametrize("kind", ["paid", "client_generation", "installation_generation"])
async def test_pending_run_cannot_survive_changed_live_authority(feeds, kind):
    row = await staged(feeds, [product()])
    if kind == "paid":
        await paid(feeds.store, 2, services=False)
    elif kind == "client_generation":
        await change(feeds.store, SyncCredentialClient, feeds.client_id, generation=2)
        feeds.token = (await mint(feeds))["access_token"]
    else:
        await change(feeds.store, SyncInstallation, feeds.iid, generation=2)
        feeds.token = (await mint(feeds))["access_token"]
    result = await service(
        feeds, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert result.status_code in {403, 409}, result.text
    async with feeds.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ResourceEvent)
                .where(ResourceEvent.resource_pk == UUID(feeds.target["resource_pk"]))
            )
            == 0
        )


async def test_membership_removal_after_destructive_approval_denies_commit(feeds):
    await committed(feeds, [product()])
    row = await staged(feeds, [])
    body = {"diff_hash": row["diff_hash"]}
    approval = await human(
        feeds, "POST", f"runs/{row['run_id']}/approvals", body=body, role="member"
    )
    assert approval.status_code == 200, approval.text
    async with feeds.factory() as db:
        member = await db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == feeds.store.wid,
                WorkspaceMember.user_id == feeds.store.users["member"],
            )
        )
        member.allowed_context_ids = []
        await db.commit()
    result = await service(
        feeds,
        "POST",
        f"runs/{row['run_id']}/commit",
        body={**body, "approval_id": approval.json()["approval_id"]},
    )
    assert result.status_code == 404, result.text
    assert (await human(feeds, "GET", "records")).json()["records"]


async def test_two_prepared_runs_cannot_commit_the_same_baseline(feeds):
    first, second = await staged(feeds, [product("one")]), await staged(feeds, [product("two")])

    async def commit(row):
        return await service(
            feeds, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
        )

    results = await asyncio.gather(commit(first), commit(second))
    assert sorted(result.status_code for result in results) == [200, 409]
    assert len((await human(feeds, "GET", "records")).json()["records"]) == 1


async def test_foreign_event_cannot_be_overwritten_or_deleted(feeds):
    await committed(feeds, [product()])
    async with feeds.factory() as db:
        record = await db.scalar(
            select(SyncRecord).where(SyncRecord.resource_pk == UUID(feeds.target["resource_pk"]))
        )
        db.add(
            ResourceEvent(
                resource_pk=record.resource_pk,
                resource_id=feeds.target["resource_id"],
                op="upsert",
                doc_id=record.doc_id,
                version=55,
                payload={"private_note": "foreign content"},
            )
        )
        await db.commit()
    row = await staged(feeds, [product(title="updated")])
    result = await service(
        feeds, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert result.status_code == 409, result.text
    async with feeds.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ResourceEvent)
                .where(ResourceEvent.resource_pk == UUID(feeds.target["resource_pk"]))
            )
            == 2
        )


async def test_same_operation_waiting_on_workspace_remains_one_durable_run(feeds):
    body = manifest(feeds, [product()])
    results = await asyncio.gather(*(service(feeds, "POST", "runs", body=body) for _ in range(2)))
    assert [result.status_code for result in results] == [201, 201]
    assert results[0].json()["run_id"] == results[1].json()["run_id"]
    async with feeds.factory() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(SyncRun).where(SyncRun.feed_id == feeds.fid)
            )
            == 1
        )
