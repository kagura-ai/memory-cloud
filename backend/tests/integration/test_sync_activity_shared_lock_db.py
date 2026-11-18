"""Activity reads share the workspace row; writers and requests still exclude them (#2914).

Every test runs a second real PostgreSQL session against the HTTP request, so the
lock modes are the database's own, not a mock's.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from models.auth import Workspace
from models.resource import Resource
from services import sync_activity
from services.workspace_locks import NOWAIT_LOCK_BACKOFF_SECONDS, lock_workspace_for_update
from tests.integration import test_sync_activity_db as fixtures
from tests.integration.sync_feed_fixtures import human
from tests.integration.sync_installation_fixtures import transfer
from utils.exceptions import ConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, activity = fixtures.store, fixtures.worker, fixtures.feeds, fixtures.activity

# Shorter than the first retry sleep plus the next two: a holder that lets go
# after this long is a "short holder" the retry outlasts.
SHORT_HOLD_SECONDS = NOWAIT_LOCK_BACKOFF_SECONDS * 4


def pause_inside_boundary(monkeypatch):
    """Hold each ``GET .../activity`` inside the boundary until ``release`` is set."""
    inside, release = asyncio.Event(), asyncio.Event()
    original = sync_activity.read_public_usage_budget

    async def paused(db, wid):
        inside.set()
        await release.wait()
        return await original(db, wid)

    monkeypatch.setattr(sync_activity, "read_public_usage_budget", paused)
    return inside, release


async def test_two_concurrent_activity_reads_are_both_inside_the_boundary(activity, monkeypatch):
    barrier = asyncio.Barrier(2)
    original = sync_activity.read_public_usage_budget

    async def meet(db, wid):
        # Both requests hold their workspace row lock here at the same time;
        # with an exclusive lock the second never arrives and the first times out.
        try:
            await asyncio.wait_for(barrier.wait(), timeout=5)
        except (TimeoutError, asyncio.BrokenBarrierError):
            pass
        return await original(db, wid)

    monkeypatch.setattr(sync_activity, "read_public_usage_budget", meet)
    first, second = await asyncio.gather(
        human(activity, "GET", "activity"),
        human(activity, "GET", "activity", role="member"),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert barrier.n_waiting == 0 and not barrier.broken


async def test_parallel_activity_routes_for_one_installation_all_succeed(activity):
    # The feed configuration reads take the resource row FOR UPDATE NOWAIT with
    # a bounded retry, which guarantees two such readers, not N: gather at most two.
    responses = await asyncio.gather(
        human(activity, "GET", "activity"),
        human(activity, "GET", "activity/runs"),
        human(activity, "GET", "activity/events"),
        human(activity, "GET", "activity", role="member"),
        human(activity, "GET", "activity/feeds", role="member"),
        human(activity, "GET", f"activity/feeds/{activity.fid}"),
    )
    assert [r.status_code for r in responses] == [200] * 6, [r.text for r in responses]


def lock_resource(holder, activity):
    """What a feed configuration read or write takes on the installation's Resource row."""
    return holder.execute(
        select(Resource.id)
        .where(Resource.id == UUID(activity.target["resource_pk"]))
        .with_for_update()
    )


async def test_feed_configuration_read_waits_out_another_open_feed_read(activity, monkeypatch):
    """The first read holds the Resource row for a moment; the second is not a 409."""
    inside = asyncio.Event()
    original = sync_activity.current_schema

    async def slow(db, installation):
        # Called with the Resource row held: keep it a short holder.
        if not inside.is_set():
            inside.set()
            await asyncio.sleep(SHORT_HOLD_SECONDS)
        return await original(db, installation)

    monkeypatch.setattr(sync_activity, "current_schema", slow)
    first = asyncio.create_task(human(activity, "GET", "activity/feeds"))
    await asyncio.wait_for(inside.wait(), timeout=10)
    second = await human(activity, "GET", f"activity/feeds/{activity.fid}", role="member")
    assert (await first).status_code == 200, (await first).text
    assert second.status_code == 200, second.text


@pytest.mark.parametrize("path", ["activity/feeds", "feed"])
async def test_feed_configuration_read_waits_out_a_short_resource_holder(activity, path):
    suffix = f"activity/feeds/{activity.fid}" if path == "feed" else path
    async with activity.factory() as holder:
        await lock_resource(holder, activity)

        async def let_go():
            await asyncio.sleep(SHORT_HOLD_SECONDS)
            await holder.rollback()

        released = asyncio.create_task(let_go())
        response = await human(activity, "GET", suffix)
        await released
    assert response.status_code == 200, response.text


async def test_feed_configuration_read_past_the_retry_window_is_still_the_409(activity):
    async with activity.factory() as holder:
        await lock_resource(holder, activity)
        response = await human(activity, "GET", "activity/feeds")
        assert response.status_code == 409, response.text
        assert response.json() == {"error": "RES-002"}
        await holder.rollback()
    assert (await human(activity, "GET", "activity/feeds")).status_code == 200


@pytest.mark.parametrize("mode", ["share", "key_share"])
async def test_activity_read_passes_a_concurrent_shared_holder(activity, mode):
    """Another reader (FOR SHARE) or an uncommitted FK insert (FOR KEY SHARE)."""
    async with activity.factory() as holder:
        await holder.execute(
            select(Workspace.id)
            .where(Workspace.id == activity.store.wid)
            .with_for_update(read=True, key_share=mode == "key_share")
        )
        response = await human(activity, "GET", "activity")
        assert response.status_code == 200, response.text
        await holder.rollback()


async def test_activity_read_waits_out_a_short_exclusive_holder(activity):
    async with activity.factory() as holder:
        await lock_workspace_for_update(holder, activity.store.wid)

        async def let_go():
            await asyncio.sleep(SHORT_HOLD_SECONDS)
            await holder.rollback()

        released = asyncio.create_task(let_go())
        response = await human(activity, "GET", "activity")
        await released
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("writer", ["for_update", "epoch_update"])
async def test_activity_read_past_the_retry_window_is_still_the_409(activity, writer):
    """Ownership, epoch and role writers take FOR UPDATE or row UPDATEs (FOR NO KEY UPDATE)."""
    async with activity.factory() as holder:
        if writer == "for_update":
            await lock_workspace_for_update(holder, activity.store.wid)
        else:
            await holder.execute(
                update(Workspace)
                .where(Workspace.id == activity.store.wid)
                .values(ownership_epoch=Workspace.ownership_epoch + 1)
            )
        response = await human(activity, "GET", "activity")
        assert response.status_code == 409, response.text
        assert response.json() == {"error": "RES-002"}
        await holder.rollback()
    assert (await human(activity, "GET", "activity")).status_code == 200


async def test_ownership_transfer_cannot_pass_an_open_activity_read(activity, monkeypatch):
    inside, release = pause_inside_boundary(monkeypatch)
    reader = asyncio.create_task(human(activity, "GET", "activity"))
    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        with pytest.raises(ConflictError, match="Workspace is being modified"):
            await transfer(activity.store, "next")
    finally:
        release.set()
    assert (await reader).status_code == 200
    async with activity.factory() as db:
        row = await db.get(Workspace, activity.store.wid)
        assert row.owner_user_id == activity.store.users["owner"]


async def test_role_change_waits_for_an_open_activity_read(activity, monkeypatch):
    from services.workspace_service import WorkspaceService

    inside, release = pause_inside_boundary(monkeypatch)
    reader = asyncio.create_task(human(activity, "GET", "activity"))

    async def demote():
        async with activity.factory() as db:
            await WorkspaceService(db).update_member_role(
                activity.store.wid,
                activity.store.users["admin"],
                "member",
                actor_user_id=activity.store.users["owner"],
            )
            await db.commit()

    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        change = asyncio.create_task(demote())
        await asyncio.sleep(0.3)
        assert not change.done()  # blocked on the reader's shared workspace row lock
    finally:
        release.set()
    assert (await reader).status_code == 200
    await asyncio.wait_for(change, timeout=10)


async def test_manual_request_keeps_its_exclusive_workspace_lock(activity):
    async with activity.factory() as holder:
        await holder.execute(
            select(Workspace.id)
            .where(Workspace.id == activity.store.wid)
            .with_for_update(read=True)
        )
        denied = await human(
            activity,
            "POST",
            "activity/requests",
            role="member",
            body={"operation_id": str(uuid4())},
        )
        assert denied.status_code == 409, denied.text
        await holder.rollback()
    created = await human(
        activity, "POST", "activity/requests", role="member", body={"operation_id": str(uuid4())}
    )
    assert created.status_code == 201, created.text
