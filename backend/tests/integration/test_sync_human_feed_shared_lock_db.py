"""Human feed reads share the workspace row; writers still exclude them (#2914).

``GET .../records`` and ``GET .../runs/{run_id}/diff`` take the workspace row
``FOR SHARE NOWAIT`` with retry, as the activity reads do. Feed configuration
writes keep their exclusive lock. Every test runs a second real PostgreSQL
session (or a second HTTP request) next to the read, so the lock modes are the
database's own, not a mock's.
"""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select, update

from models.auth import Context, Workspace
from models.resource import Resource
from services import sync_activity, sync_feed_human
from services.workspace_locks import NOWAIT_LOCK_BACKOFF_SECONDS, lock_workspace_for_update
from tests.integration import test_sync_activity_db as fixtures
from tests.integration.sync_feed_fixtures import committed, human, product, staged
from tests.integration.sync_installation_fixtures import transfer
from utils.exceptions import ConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, activity = fixtures.store, fixtures.worker, fixtures.feeds, fixtures.activity

# A holder that lets go after this long is one the retry outlasts.
SHORT_HOLD_SECONDS = NOWAIT_LOCK_BACKOFF_SECONDS * 4


def pause_human_read(monkeypatch):
    """Hold each human feed read inside its boundary until ``release`` is set."""
    inside, release = asyncio.Event(), asyncio.Event()
    original = sync_feed_human.current_schema

    async def paused(db, installation):
        inside.set()
        await release.wait()
        return await original(db, installation)

    monkeypatch.setattr(sync_feed_human, "current_schema", paused)
    return inside, release


async def test_two_concurrent_human_feed_reads_both_succeed(feeds, monkeypatch):
    """The first read is still open when the second arrives; neither is a 409."""
    run = await staged(feeds, [product("one")])
    inside = asyncio.Event()
    original = sync_feed_human.current_schema

    async def slow(db, installation):
        # Called inside the boundary: keep the first read open for a moment.
        if not inside.is_set():
            inside.set()
            await asyncio.sleep(SHORT_HOLD_SECONDS)
        return await original(db, installation)

    monkeypatch.setattr(sync_feed_human, "current_schema", slow)
    first = asyncio.create_task(human(feeds, "GET", "records"))
    await asyncio.wait_for(inside.wait(), timeout=10)
    second = await human(feeds, "GET", f"runs/{run['run_id']}/diff", role="member")
    assert (await first).status_code == 200, (await first).text
    assert second.status_code == 200, second.text


async def test_two_gathered_human_feed_reads_for_one_installation_both_succeed(feeds):
    # Each human read still takes the installation's resource and context rows
    # FOR UPDATE NOWAIT with a bounded retry, so the reads take turns there. The
    # retry guarantees that two readers both get in, not N: do not gather more.
    run = await staged(feeds, [product("one")])
    responses = await asyncio.gather(
        human(feeds, "GET", "records"),
        human(feeds, "GET", f"runs/{run['run_id']}/diff", role="member"),
    )
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]


async def test_human_feed_read_and_activity_read_are_inside_together(activity, monkeypatch):
    await committed(activity, [product("one")])
    barrier = asyncio.Barrier(2)
    met: list[str] = []

    async def meet(name):
        # Both requests hold their workspace row lock here at the same time;
        # with an exclusive lock on either side the other never arrives.
        try:
            await asyncio.wait_for(barrier.wait(), timeout=5)
            met.append(name)
        except (TimeoutError, asyncio.BrokenBarrierError):
            pass

    schema, budget = sync_feed_human.current_schema, sync_activity.read_public_usage_budget

    async def human_side(db, installation):
        await meet("records")
        return await schema(db, installation)

    async def activity_side(db, wid):
        await meet("activity")
        return await budget(db, wid)

    monkeypatch.setattr(sync_feed_human, "current_schema", human_side)
    monkeypatch.setattr(sync_activity, "read_public_usage_budget", activity_side)
    records, overview = await asyncio.gather(
        human(activity, "GET", "records"),
        human(activity, "GET", "activity", role="member"),
    )
    assert records.status_code == 200, records.text
    assert overview.status_code == 200, overview.text
    assert sorted(met) == ["activity", "records"]


@pytest.mark.parametrize("mode", ["share", "key_share"])
async def test_human_feed_read_passes_a_concurrent_shared_workspace_holder(feeds, mode):
    """Another reader (FOR SHARE) or an uncommitted FK insert (FOR KEY SHARE)."""
    await committed(feeds, [product("one")])
    async with feeds.factory() as holder:
        await holder.execute(
            select(Workspace.id)
            .where(Workspace.id == feeds.store.wid)
            .with_for_update(read=True, key_share=mode == "key_share")
        )
        response = await human(feeds, "GET", "records")
        assert response.status_code == 200, response.text
        await holder.rollback()


def lock_row(holder, feeds, row):
    """What a feed configuration write takes on the installation's Resource or Context row."""
    model, key = (
        (Resource, feeds.target["resource_pk"])
        if row == "resource"
        else (Context, feeds.target["context_id"])
    )
    return holder.execute(select(model.id).where(model.id == UUID(key)).with_for_update())


@pytest.mark.parametrize("row", ["workspace", "resource", "context"])
async def test_human_feed_read_waits_out_a_short_exclusive_holder(feeds, row):
    await committed(feeds, [product("one")])
    async with feeds.factory() as holder:
        if row == "workspace":
            await lock_workspace_for_update(holder, feeds.store.wid)
        else:
            await lock_row(holder, feeds, row)

        async def let_go():
            await asyncio.sleep(SHORT_HOLD_SECONDS)
            await holder.rollback()

        released = asyncio.create_task(let_go())
        response = await human(feeds, "GET", "records")
        await released
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("writer", ["for_update", "epoch_update", "resource", "context"])
async def test_human_feed_read_past_the_retry_window_is_still_the_409(feeds, writer):
    """Ownership, epoch and role writers and feed writes keep excluding the read."""
    await committed(feeds, [product("one")])
    async with feeds.factory() as holder:
        if writer == "for_update":
            await lock_workspace_for_update(holder, feeds.store.wid)
        elif writer == "epoch_update":
            await holder.execute(
                update(Workspace)
                .where(Workspace.id == feeds.store.wid)
                .values(ownership_epoch=Workspace.ownership_epoch + 1)
            )
        else:
            await lock_row(holder, feeds, writer)
        response = await human(feeds, "GET", "records")
        assert response.status_code == 409, response.text
        assert response.json() == {"error": "RES-002"}
        await holder.rollback()
    assert (await human(feeds, "GET", "records")).status_code == 200


async def test_ownership_transfer_cannot_pass_an_open_human_feed_read(feeds, monkeypatch):
    await committed(feeds, [product("one")])
    inside, release = pause_human_read(monkeypatch)
    reader = asyncio.create_task(human(feeds, "GET", "records"))
    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        with pytest.raises(ConflictError, match="Workspace is being modified"):
            await transfer(feeds.store, "next")
    finally:
        release.set()
    assert (await reader).status_code == 200
    async with feeds.factory() as db:
        row = await db.get(Workspace, feeds.store.wid)
        assert row.owner_user_id == feeds.store.users["owner"]


async def test_feed_configuration_write_cannot_pass_an_open_human_feed_read(feeds, monkeypatch):
    await committed(feeds, [product("one")])
    inside, release = pause_human_read(monkeypatch)
    reader = asyncio.create_task(human(feeds, "GET", "records", role="member"))
    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        write = await human(
            feeds, "PUT", f"feeds/{feeds.fid}", body={"kind": "product", "config_hash": "3" * 64}
        )
        assert write.status_code == 409, write.text
    finally:
        release.set()
    assert (await reader).status_code == 200


async def test_feed_configuration_write_keeps_its_exclusive_workspace_lock(feeds):
    async with feeds.factory() as holder:
        await holder.execute(
            select(Workspace.id).where(Workspace.id == feeds.store.wid).with_for_update(read=True)
        )
        denied = await human(
            feeds, "PUT", f"feeds/{feeds.fid}", body={"kind": "product", "config_hash": "3" * 64}
        )
        assert denied.status_code == 409, denied.text
        await holder.rollback()
    allowed = await human(
        feeds, "PUT", f"feeds/{feeds.fid}", body={"kind": "product", "config_hash": "3" * 64}
    )
    assert allowed.status_code == 200, allowed.text
