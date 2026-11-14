"""Observed PostgreSQL contention and actual commit-time expiry, not mocked locks."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from cli.sync_credentials import revoke_key
from models.auth import Context
from models.sync_credential import SyncAccessToken, SyncAssertionUse
from services import integration_entitlements
from services import sync_credentials as service
from tests.integration import sync_credential_fixtures as fixtures
from tests.integration.sync_credential_fixtures import mint, token_form
from tests.integration.sync_installation_fixtures import paid
from tests.integration.test_alembic_migrations import _sync_engine
from tests.integration.test_sync_installation_concurrency_db import wait_blocked
from utils.exceptions import AuthorizationError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store
worker = fixtures.worker


async def context_name(worker):
    async with worker.factory() as db:
        return await db.scalar(
            select(Context.display_name).where(Context.id == UUID(worker.target["context_id"]))
        )


async def paused_write(worker, value):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def perform():
        async with worker.factory() as db:
            async with service.sync_token_boundary(
                db, value, UUID(worker.target["installation_id"]), operation="records:write"
            ) as authority:
                authority.context.display_name = "Committed worker change"
                await db.flush()
                entered.set()
                await finish.wait()

    task = asyncio.create_task(perform())
    await asyncio.wait_for(entered.wait(), 8)
    return task, finish


async def test_concurrent_assertions_have_one_durable_winner_with_real_unique_index_wait(
    worker, monkeypatch
):
    original = service.isolated_session
    entered, finish, pids = asyncio.Event(), asyncio.Event(), asyncio.Queue()

    @asynccontextmanager
    async def replay_session(db):
        async with original(db) as replay:
            await pids.put(await replay.scalar(text("SELECT pg_backend_pid()")))
            commit = replay.commit

            async def paused_commit():
                entered.set()
                await finish.wait()
                await commit()

            monkeypatch.setattr(replay, "commit", paused_commit)
            yield replay

    monkeypatch.setattr(service, "isolated_session", replay_session)
    form = token_form(worker)

    async def attempt():
        async with worker.factory() as db:
            return await service.issue_token(db, form)

    first = asyncio.create_task(attempt())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 8)
        await pids.get()
        second = asyncio.create_task(attempt())
        pid = await asyncio.wait_for(pids.get(), 8)
        await wait_blocked(worker.store, pid)
        finish.set()
        winner = await asyncio.wait_for(first, 8)
        assert winner["expires_in"] == 300
        with pytest.raises(service.SyncCredentialError, match="invalid_client"):
            await asyncio.wait_for(second, 8)
        async with worker.factory() as db:
            assert len((await db.scalars(select(SyncAssertionUse))).all()) == 1
            assert len((await db.scalars(select(SyncAccessToken))).all()) == 1
    finally:
        finish.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)


async def test_key_revocation_waits_for_admitted_write_and_then_fences_old_token(worker):
    result = await mint(worker)
    task, finish = await paused_write(worker, result["access_token"])
    engine, pids = _sync_engine(), asyncio.Queue()
    loop = asyncio.get_running_loop()

    def revoke():
        with Session(engine) as db, db.begin():
            pid = db.scalar(text("SELECT pg_backend_pid()"))
            loop.call_soon_threadsafe(pids.put_nowait, pid)
            revoke_key(db, worker.client_id, "current")

    revoker = asyncio.create_task(asyncio.to_thread(revoke))
    try:
        pid = await asyncio.wait_for(pids.get(), 8)
        await wait_blocked(worker.store, pid)
        finish.set()
        await asyncio.wait_for(task, 8)
        await asyncio.wait_for(revoker, 8)
        assert await context_name(worker) == "Committed worker change"
        async with worker.factory() as db:
            with pytest.raises(service.SyncCredentialError):
                async with service.sync_token_boundary(
                    db,
                    result["access_token"],
                    UUID(worker.target["installation_id"]),
                    operation="records:write",
                ):
                    pytest.fail("Old key wrote after revocation committed")
    finally:
        finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, revoker, return_exceptions=True)
        engine.dispose()


async def test_paid_revocation_waits_for_admitted_write_and_blocks_the_next_one(
    worker, monkeypatch
):
    result = await mint(worker)
    task, finish = await paused_write(worker, result["access_token"])
    original, pids = integration_entitlements._workspace, asyncio.Queue()

    async def observed(db, wid, *, lock):
        if lock:
            await pids.put(await db.scalar(text("SELECT pg_backend_pid()")))
        return await original(db, wid, lock=lock)

    monkeypatch.setattr(integration_entitlements, "_workspace", observed)
    revoker = asyncio.create_task(paid(worker.store, 2, services=False))
    try:
        pid = await asyncio.wait_for(pids.get(), 8)
        await wait_blocked(worker.store, pid)
        finish.set()
        await asyncio.wait_for(task, 8)
        await asyncio.wait_for(revoker, 8)
        async with worker.factory() as db:
            with pytest.raises(AuthorizationError):
                async with service.sync_token_boundary(
                    db,
                    result["access_token"],
                    UUID(worker.target["installation_id"]),
                    operation="records:write",
                ):
                    pytest.fail("Unpaid operation admitted")
    finally:
        finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, revoker, return_exceptions=True)


@pytest.mark.parametrize("boundary", ["token", "paid", "assertion"])
async def test_expiry_at_actual_before_commit_rolls_back_all_but_replay_receipt(worker, boundary):
    if boundary == "paid":
        await paid(worker.store, 2, end=worker.store.clock.now + timedelta(seconds=60))
    token = await mint(worker) if boundary != "assertion" else None
    original = await context_name(worker)
    async with worker.factory() as db:

        def expire(_session):
            worker.store.clock.now += timedelta(seconds=301 if boundary == "token" else 61)

        event.listen(db.sync_session, "before_commit", expire)
        try:
            with pytest.raises(service.SyncCredentialError):
                if boundary == "assertion":
                    await service.issue_token(db, token_form(worker))
                else:
                    async with service.sync_token_boundary(
                        db,
                        token["access_token"],
                        UUID(worker.target["installation_id"]),
                        operation="records:write",
                    ) as authority:
                        authority.context.display_name = "Must roll back"
        finally:
            event.remove(db.sync_session, "before_commit", expire)
    assert await context_name(worker) == original
    if boundary == "assertion":
        async with worker.factory() as db:
            assert len((await db.scalars(select(SyncAssertionUse))).all()) == 1
            assert not (await db.scalars(select(SyncAccessToken))).all()


@pytest.mark.parametrize("prior", ["insert", "lock", "key_share"])
async def test_credential_entry_refuses_prior_writes_or_locks_without_rolling_back_caller(
    worker, prior
):
    async with worker.factory() as db:
        if prior == "insert":
            db.add(
                Context(
                    workspace_id=worker.store.wid, name="earlier-caller-write", is_private=False
                )
            )
            await db.flush()
        else:
            stmt = select(Context).where(Context.id == UUID(worker.target["context_id"]))
            await db.scalar(
                stmt.with_for_update(key_share=prior == "key_share", read=prior == "key_share")
            )
        with pytest.raises(RuntimeError, match="writes and row locks"):
            await service.issue_token(db, token_form(worker))
        assert await db.scalar(text("SELECT pg_current_xact_id_if_assigned()")) is not None
        assert not db.info.get("paid_integration_write")
        await db.rollback()


async def test_cancellation_rolls_back_and_releases_all_authority_locks(worker):
    result = await mint(worker)
    original = await context_name(worker)
    task, finish = await paused_write(worker, result["access_token"])
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await context_name(worker) == original
        async with worker.factory() as db:
            async with service.sync_token_boundary(
                db,
                result["access_token"],
                UUID(worker.target["installation_id"]),
                operation="results:read",
            ) as authority:
                assert authority.installation.id == UUID(worker.target["installation_id"])
                assert authority.context.display_name == original
    finally:
        finish.set()


async def test_key_revocation_started_first_refuses_admission_then_permanently_fences_token(worker):
    result = await mint(worker)
    original = await context_name(worker)
    engine, staged, finish = _sync_engine(), asyncio.Event(), Event()
    loop = asyncio.get_running_loop()

    def revoke():
        with Session(engine) as db, db.begin():
            revoke_key(db, worker.client_id, "current")
            db.flush()
            loop.call_soon_threadsafe(staged.set)
            if not finish.wait(15):
                raise RuntimeError("Test did not release key revocation")

    task = asyncio.create_task(asyncio.to_thread(revoke))
    try:
        await asyncio.wait_for(staged.wait(), 8)
        async with worker.factory() as db:
            with pytest.raises(service.SyncCredentialError) as error:
                async with service.sync_token_boundary(
                    db,
                    result["access_token"],
                    UUID(worker.target["installation_id"]),
                    operation="records:write",
                ):
                    pytest.fail("Changing key authority admitted a write")
            assert error.value.status == 503
        finish.set()
        await asyncio.wait_for(task, 8)
        async with worker.factory() as db:
            with pytest.raises(service.SyncCredentialError) as error:
                async with service.sync_token_boundary(
                    db,
                    result["access_token"],
                    UUID(worker.target["installation_id"]),
                    operation="records:write",
                ):
                    pytest.fail("Revoked key admitted a write")
            assert error.value.status == 401
        assert await context_name(worker) == original
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        engine.dispose()
