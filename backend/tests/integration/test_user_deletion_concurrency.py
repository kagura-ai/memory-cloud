"""Ownership writers cannot race past the administrative deletion snapshot."""

import asyncio
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes.admin import delete_user
from models.auth import Context, User, Workspace
from services.context_service import ContextService
from services.user_deletion_service import delete_user_rows
from services.workspace_ownership_service import WorkspaceOwnershipService
from services.workspace_service import WorkspaceService
from utils.exceptions import BadRequestError, ConflictError, ValidationError


async def wait_for_block(maker, pid):
    async with maker() as observer:
        while not await observer.scalar(
            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
        ):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["context", "workspace", "transfer", "force"])
async def test_new_ownership_waits_for_deletion(db_session, connector_world, operation):
    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    ws = await world.workspace(owner, **{target: "admin"})
    ws_id = ws.id
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gap, resume, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writer_pid = None

    async def pause_cleanup(db, subject, **kwargs):
        gap.set()
        await asyncio.wait_for(resume.wait(), 15)
        return await delete_user_rows(db, subject, **kwargs)

    async def remove():
        async with maker() as db:
            await delete_user(target, {"user_id": "operator"}, db)

    async def write():
        nonlocal writer_pid
        async with maker() as db:
            writer_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            if operation == "context":
                await ContextService(db).create_context(
                    workspace_id=ws_id,
                    name="late-context",
                    created_by=target,
                    create_collection=False,
                )
            elif operation == "workspace":
                await WorkspaceService(db).create_workspace(
                    name="late-workspace", owner_user_id=target
                )
            elif operation == "transfer":
                await WorkspaceOwnershipService(db).transfer_ownership(
                    workspace_id=ws_id,
                    current_owner_id=owner,
                    target_user_id=target,
                    performed_by_email="owner@example.test",
                )
            else:
                await WorkspaceOwnershipService(db).force_transfer_ownership(
                    workspace_id=ws_id,
                    target_user_id=target,
                    performed_by_user_id="operator",
                    performed_by_email="operator@example.test",
                    reason="Test concurrent deletion",
                )

    tasks = []
    with patch("services.user_deletion_service.delete_user_rows", pause_cleanup):
        try:
            deletion = asyncio.create_task(remove())
            tasks.append(deletion)
            await asyncio.wait_for(gap.wait(), 15)
            writer = asyncio.create_task(write())
            tasks.append(writer)
            await asyncio.wait_for(started.wait(), 15)
            await asyncio.wait_for(wait_for_block(maker, writer_pid), 5)
            resume.set()
            await asyncio.wait_for(deletion, 15)
            with pytest.raises((BadRequestError, ValidationError), match="no longer exists"):
                await asyncio.wait_for(writer, 15)
        finally:
            resume.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) is None
    assert await db_session.scalar(select(Context.id).where(Context.created_by == target)) is None
    assert (
        await db_session.scalar(select(Workspace.id).where(Workspace.owner_user_id == target))
        is None
    )
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == ws_id))
        == owner
    )


@pytest.mark.asyncio
async def test_unexpected_late_context_rejects_cleanup_without_deleting_it(
    db_session, connector_world
):
    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    ws = await world.workspace(owner, **{target: "admin"})
    ws_id, context_id = ws.id, uuid4()
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def add_late_context(db, subject, **kwargs):
        # Simulate a legacy writer bypassing the supported service fence.
        async with maker() as other:
            other.add(Context(id=context_id, workspace_id=ws_id, name="late", created_by=target))
            await other.commit()
        return await delete_user_rows(db, subject, **kwargs)

    with patch("services.user_deletion_service.delete_user_rows", add_late_context):
        with pytest.raises(HTTPException) as error:
            await delete_user(target, {"user_id": "operator"}, db_session)
    assert error.value.status_code == 409
    assert await db_session.get(Context, context_id) is not None
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) == target


@pytest.mark.asyncio
async def test_transfer_refuses_busy_workspace_before_preference_update(
    db_session, connector_world
):
    world = connector_world
    owner = await world.user("owner")
    target = await world.user("successor")
    ws = await world.workspace(owner, **{target: "admin"})
    ws_id = ws.id
    await db_session.execute(
        update(User).where(User.user_id == target).values(current_workspace_id=ws_id)
    )
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with maker() as deleting, maker() as transferring:
        await deleting.execute(select(Workspace.id).where(Workspace.id == ws_id).with_for_update())
        with pytest.raises(ConflictError, match="being modified"):
            await asyncio.wait_for(
                WorkspaceOwnershipService(transferring).transfer_ownership(
                    workspace_id=ws_id,
                    current_owner_id=owner,
                    target_user_id=target,
                    performed_by_email="owner@example.test",
                ),
                5,
            )
        await transferring.rollback()
        # Soft-delete's workspace -> user preference update can now finish.
        await deleting.execute(
            update(User).where(User.user_id == target).values(current_workspace_id=None)
        )
        await deleting.commit()
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == ws_id))
        == owner
    )


@pytest.mark.asyncio
async def test_erasure_auto_transfer_rechecks_successor_after_admin_deletion(
    db_session, connector_world
):
    from services.account_erasure_service import AccountErasureService

    world = connector_world
    target = await world.user("departing")
    owner = await world.user("erasure-owner")
    ws = await world.workspace(owner, **{target: "admin"})
    ws_id = ws.id
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gap, resume, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    transfer_pid = None

    async def pause_cleanup(db, subject, **kwargs):
        gap.set()
        await asyncio.wait_for(resume.wait(), 15)
        return await delete_user_rows(db, subject, **kwargs)

    async def remove():
        async with maker() as db:
            await delete_user(target, {"user_id": "operator"}, db)

    async def auto_transfer():
        nonlocal transfer_pid
        async with maker() as db:
            transfer_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            await db.execute(select(User.id).where(User.user_id == owner).with_for_update())
            owned = list(
                (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalars()
            )
            started.set()
            return await AccountErasureService(db)._handle_owned_workspaces(owner, owned)

    tasks = []
    with patch("services.user_deletion_service.delete_user_rows", pause_cleanup):
        try:
            deletion = asyncio.create_task(remove())
            tasks.append(deletion)
            await asyncio.wait_for(gap.wait(), 15)
            transfer = asyncio.create_task(auto_transfer())
            tasks.append(transfer)
            await asyncio.wait_for(started.wait(), 15)
            await asyncio.wait_for(wait_for_block(maker, transfer_pid), 5)
            resume.set()
            await asyncio.wait_for(deletion, 15)
            result = await asyncio.wait_for(transfer, 15)
            assert result["transferred"] == []
        finally:
            resume.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == ws_id))
        == owner
    )
