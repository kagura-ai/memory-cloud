"""A rotation cannot mint into the re-own/delete gap (#2370)."""

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes.admin import delete_user
from auth.api_keys import APIKeyManager
from models.auth import APIKey
from services.connector_key_ownership import reown_connector_keys
from services.connector_provisioning import ConnectorProvisioningService
from services.workspace_service import WorkspaceService


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["admin", "member"])
async def test_rotation_waits_across_reown_and_delete(db_session, connector_world, operation):
    world = connector_world
    target = await world.user("leaving")
    owner = await world.user("owner")
    ws = await world.workspace(owner, **{target: "admin"})
    connector = (await world.connector(ws, by=target)).connector
    ws_id, connector_id = ws.id, connector.id
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gap, resume, rotating = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rotation_pid = None

    async def pause_after_reown(db, subject, **kwargs):
        count = await reown_connector_keys(db, subject, **kwargs)
        gap.set()
        await asyncio.wait_for(resume.wait(), 15)
        return count

    async def remove():
        async with maker() as db:
            if operation == "admin":
                await delete_user(target, {"user_id": "operator"}, db)
            else:
                await WorkspaceService(db).remove_member(ws_id, target)

    async def rotate():
        nonlocal rotation_pid
        async with maker() as db:
            rotation_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            rotating.set()
            result = await ConnectorProvisioningService(db).rotate_kmc_key(
                ws_id, connector_id, owner
            )
            await db.commit()
            return result

    async def wait_for_db_block():
        # Observe PostgreSQL's actual wait edge, not a timing assumption.
        async with maker() as observer:
            while not await observer.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": rotation_pid}
            ):
                await asyncio.sleep(0.01)

    module = (
        "services.user_deletion_service"
        if operation == "admin"
        else "services.member_credentials_service"
    )
    tasks = []
    with patch(f"{module}.reown_connector_keys", pause_after_reown):
        try:
            deletion = asyncio.create_task(remove())
            tasks.append(deletion)
            await asyncio.wait_for(gap.wait(), 15)
            rotation = asyncio.create_task(rotate())
            tasks.append(rotation)
            await asyncio.wait_for(rotating.wait(), 15)
            await asyncio.wait_for(wait_for_db_block(), 5)
            assert not rotation.done()
            resume.set()
            await asyncio.wait_for(deletion, 15)
            result = await asyncio.wait_for(rotation, 15)
        finally:
            resume.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert result.key_owner_user_id == owner
    active = list(
        (
            await db_session.execute(
                select(APIKey).where(
                    APIKey.connector_id == connector_id, APIKey.revoked_at.is_(None)
                )
            )
        ).scalars()
    )
    assert len(active) == 1 and active[0].user_id == owner
    verified = await APIKeyManager(db_session).verify_key(result.plaintext_kmc_api_key)
    assert verified is not None and verified.user_id == owner


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["admin", "member"])
async def test_deletion_waits_for_rotation_and_reowns_the_replacement(
    db_session, connector_world, operation
):
    world = connector_world
    target = await world.user("leaving")
    owner = await world.user("owner")
    ws = await world.workspace(owner, **{target: "admin"})
    connector = (await world.connector(ws, by=target)).connector
    ws_id, connector_id = ws.id, connector.id
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    from .test_user_deletion_concurrency import wait_for_block

    started = asyncio.Event()
    deletion_pid = None

    async def remove():
        nonlocal deletion_pid
        async with maker() as db:
            deletion_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            if operation == "admin":
                await delete_user(target, {"user_id": "operator"}, db)
            else:
                await WorkspaceService(db).remove_member(ws_id, target)

    task = None
    try:
        async with maker() as rotating:
            result = await ConnectorProvisioningService(rotating).rotate_kmc_key(
                ws_id, connector_id, owner
            )
            assert result.key_owner_user_id == target
            task = asyncio.create_task(remove())
            await asyncio.wait_for(started.wait(), 15)
            await asyncio.wait_for(wait_for_block(maker, deletion_pid), 5)
            await rotating.commit()
        await asyncio.wait_for(task, 15)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    verified = await APIKeyManager(db_session).verify_key(result.plaintext_kmc_api_key)
    assert verified is not None and verified.user_id == owner
