"""One lock order for account erasure and user deletion (#2418).

The global order: ``users`` rows → the identity-link advisory lock → live
workspace rows (``NOWAIT`` while a ``users`` row is held) → soft-deleted
workspace rows → contexts → memories → child rows. A transaction holding a
``users`` row never waits on a workspace row.

``DELETE /workspaces/W`` (``WorkspaceService.delete_workspace``) takes W first
and the ``users`` rows after (``current_workspace_id``). Erasure holds the
subject's ``users`` row from its first step, so at steps 2 and 3 it takes the
workspace rows ``NOWAIT`` and reschedules the request when one is busy.
Steps 2 and 3 are one transaction: step 2 reads the owned workspaces under the
identity-link lock and nothing is committed before step 3's commit.
Each test drives two real Postgres sessions and waits on
``pg_blocking_pids`` before releasing the paused one.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes.admin import delete_user
from models.auth import User, Workspace, WorkspaceMember
from models.erasure import (
    REASON_USER_REQUEST_VIA_SUPPORT,
    STATUS_COOLING_OFF,
    STATUS_FAILED,
    ErasureRequest,
)
from services.account_erasure_service import AccountErasureService
from services.file_storage_service import FileStorageService
from services.workspace_service import WorkspaceService
from utils.exceptions import ConflictError, ErasureRetryScheduledError

from .test_delete_admin_cli_db import (  # noqa: F401 (fixtures)
    _password_admin,
    sole_password_admin,
    transfer_audits,
)

pytestmark = pytest.mark.timeout(90)


async def _pid(db) -> int:
    return await db.scalar(text("SELECT pg_backend_pid()"))


async def _is_blocked(maker, pid: int) -> bool:
    async with maker() as observer:
        return bool(
            await observer.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            )
        )


async def _done_or_waiting_on(maker, task: asyncio.Task, holder: dict, timeout: float = 15) -> None:
    """Until ``task`` finished or some backend waits on a lock the paused
    workspace delete (``holder["pid"]``) holds. ``task`` may run several
    transactions (on as many connections), so the waiter is found from the
    holder's side."""

    async def poll() -> None:
        while not task.done():
            async with maker() as observer:
                if await observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :pid = ANY(pg_blocking_pids(pid)))"
                    ),
                    {"pid": holder["pid"]},
                ):
                    return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


async def _until_blocked(maker, pid_of, timeout: float = 15) -> None:
    async def poll() -> None:
        while True:
            pid = pid_of()
            if pid is not None and await _is_blocked(maker, pid):
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


@contextmanager
def _workspace_delete_paused(at: str = "lock"):
    """``delete_workspace`` stops holding its workspace row: right after it
    locked it (``at="lock"``: nothing else held yet), or after it deleted its
    external keys and purged its files (``at="purge"``: the users rows it
    clears are locked by then, #2418; before, it locked them only at the end)."""
    import services.workspace_service as workspace_module

    holding, release = asyncio.Event(), asyncio.Event()
    holder: dict = {}

    async def pause(db) -> None:
        holder["pid"] = await _pid(db)
        holding.set()
        await asyncio.wait_for(release.wait(), 30)

    if at == "lock":
        original_lock = workspace_module.lock_workspace_for_update

        async def paused_lock(db, workspace_id, **kwargs):
            workspace = await original_lock(db, workspace_id, **kwargs)
            await pause(db)
            return workspace

        with patch.object(workspace_module, "lock_workspace_for_update", paused_lock):
            yield holding, release, holder
        return

    original_purge = FileStorageService.purge_workspace_files

    async def paused_purge(self, workspace_id, **kwargs):
        result = await original_purge(self, workspace_id, **kwargs)
        await pause(self.db)
        return result

    with patch.object(FileStorageService, "purge_workspace_files", paused_purge):
        yield holding, release, holder


@contextmanager
def _external_stores_stubbed():
    """The vector store, Redis and the session store, as in
    ``test_account_erasure_integration.patched_external_stores``."""
    with (
        patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(return_value={"kagura_memories": 0}),
        ),
        patch(
            "services.account_erasure_service.clear_co_activations",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "services.account_erasure_service.clear_user_rate_limits",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "services.account_erasure_service.get_redis_client",
            new=MagicMock(return_value=MagicMock(setex=AsyncMock(), delete=AsyncMock())),
        ),
        patch("services.account_erasure_service.get_session_manager", new=lambda: None),
        patch(
            "services.security_notification_service.purge_user_notification_state",
            AsyncMock(return_value=0),
        ),
        patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()),
    ):
        yield


async def _cancel_all(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _current_workspace(db, user_id: str, workspace_id) -> None:
    await db.execute(
        update(User).where(User.user_id == user_id).values(current_workspace_id=workspace_id)
    )


# ---------------------------------------------------------------------------
# Cycle B: erasure step 2 against the subject's own DELETE /workspaces/W
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_two_refuses_a_workspace_its_delete_holds(db_session, connector_world):
    """The workspace delete holds W and will update ``users[S]``; erasure
    holds ``users[S]`` and the link lock. Step 2 must not wait on W: it
    raises ``ConflictError`` at once and the delete completes."""
    world = connector_world
    subject = await world.user("subject")
    successor = await world.user("successor")
    ws = await world.workspace(subject, **{successor: "admin"})
    ws_id = ws.id
    await _current_workspace(db_session, subject, ws_id)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def delete_ws() -> None:
        async with maker() as db:
            await WorkspaceService(db).delete_workspace(ws_id, deleted_by=subject)

    async def step_two() -> dict:
        async with maker() as db:
            try:
                await db.execute(select(User.id).where(User.user_id == subject).with_for_update())
                return await AccountErasureService(db)._handle_owned_workspaces(subject)
            finally:
                await db.rollback()

    tasks: list[asyncio.Task] = []
    with _workspace_delete_paused() as (holding, release, holder):
        try:
            deleting = asyncio.create_task(delete_ws())
            tasks.append(deleting)
            await asyncio.wait_for(holding.wait(), 15)
            erasing = asyncio.create_task(step_two())
            tasks.append(erasing)
            await _done_or_waiting_on(maker, erasing, holder)
            release.set()
            erasure_result, delete_result = await asyncio.wait_for(
                asyncio.gather(erasing, deleting, return_exceptions=True), 30
            )
        finally:
            release.set()
            await _cancel_all(tasks)

    assert isinstance(erasure_result, ConflictError), erasure_result
    assert delete_result is None, delete_result
    row = (
        await db_session.execute(
            select(Workspace.owner_user_id, Workspace.deleted_at).where(Workspace.id == ws_id)
        )
    ).one()
    assert row.owner_user_id == subject  # nothing was handed on
    assert row.deleted_at is not None
    current = await db_session.scalar(
        select(User.current_workspace_id).where(User.user_id == subject)
    )
    assert current is None


@pytest.mark.asyncio
async def test_step_two_that_got_the_workspace_first_finishes_its_transfer(
    db_session, connector_world
):
    """The other interleaving: erasure locked W first. The delete waits on W
    (holding nothing), the transfer commits, then the delete runs on the
    workspace the successor now owns."""
    import services.account_erasure_service as erasure_module

    world = connector_world
    subject = await world.user("subject")
    successor = await world.user("successor")
    ws = await world.workspace(subject, **{successor: "admin"})
    ws_id = ws.id
    await _current_workspace(db_session, subject, ws_id)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holding, release = asyncio.Event(), asyncio.Event()
    delete_pid: int | None = None
    original = erasure_module.lock_workspace_for_update

    async def paused_lock(db, workspace_id, **kwargs):
        workspace = await original(db, workspace_id, **kwargs)
        holding.set()
        await asyncio.wait_for(release.wait(), 30)
        return workspace

    async def step_two() -> dict:
        async with maker() as db:
            await db.execute(select(User.id).where(User.user_id == subject).with_for_update())
            result = await AccountErasureService(db)._handle_owned_workspaces(subject)
            await db.commit()
            return result

    async def delete_ws() -> None:
        nonlocal delete_pid
        async with maker() as db:
            delete_pid = await _pid(db)
            await WorkspaceService(db).delete_workspace(ws_id, deleted_by=subject)

    tasks: list[asyncio.Task] = []
    with patch.object(erasure_module, "lock_workspace_for_update", paused_lock):
        try:
            erasing = asyncio.create_task(step_two())
            tasks.append(erasing)
            await asyncio.wait_for(holding.wait(), 15)
            deleting = asyncio.create_task(delete_ws())
            tasks.append(deleting)
            await _until_blocked(maker, lambda: delete_pid)
            release.set()
            erasure_result, delete_result = await asyncio.wait_for(
                asyncio.gather(erasing, deleting, return_exceptions=True), 30
            )
        finally:
            release.set()
            await _cancel_all(tasks)

    assert delete_result is None, delete_result
    assert not isinstance(erasure_result, BaseException), erasure_result
    assert erasure_result["transferred"] == [
        {"workspace_id": str(ws_id), "new_owner_user_id": successor}
    ]
    owner = await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == ws_id))
    assert owner == successor


# ---------------------------------------------------------------------------
# Cycle C: erasure step 3 against the same workspace delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_three_reschedules_on_a_workspace_its_delete_holds(db_session, connector_world):
    """A sole-owned W: step 2 has nothing to hand on, and step 3's deletes
    of W's rows would wait on the delete that holds W while erasure holds
    ``users[S]``. Step 3 takes the owned workspaces ``NOWAIT`` first and the
    request is rescheduled; nothing is erased and the delete completes. The
    shared workspace step 2 handed on is rolled back with step 3: one
    transaction, so the hand-over is not left behind."""
    world = connector_world
    subject = await world.user("subject")
    successor = await world.user("successor")
    ws = await world.workspace(subject)
    ws_id = ws.id
    shared_id = (await world.workspace(subject, **{successor: "admin"})).id
    await _current_workspace(db_session, subject, ws_id)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def delete_ws() -> None:
        async with maker() as db:
            await WorkspaceService(db).delete_workspace(ws_id, deleted_by=subject)

    async def erase() -> None:
        async with maker() as db:
            await AccountErasureService(db).admin_force_erase(
                target_user_id=subject,
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test",
            )

    tasks: list[asyncio.Task] = []
    with _external_stores_stubbed(), _workspace_delete_paused() as (holding, release, holder):
        try:
            deleting = asyncio.create_task(delete_ws())
            tasks.append(deleting)
            await asyncio.wait_for(holding.wait(), 15)
            erasing = asyncio.create_task(erase())
            tasks.append(erasing)
            await _done_or_waiting_on(maker, erasing, holder)
            release.set()
            erasure_result, delete_result = await asyncio.wait_for(
                asyncio.gather(erasing, deleting, return_exceptions=True), 30
            )
        finally:
            release.set()
            await _cancel_all(tasks)

    assert isinstance(erasure_result, ErasureRetryScheduledError), erasure_result
    assert delete_result is None, delete_result
    request = (
        await db_session.execute(
            select(ErasureRequest)
            .where(ErasureRequest.user_id == subject)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert request.status == STATUS_COOLING_OFF
    assert request.status != STATUS_FAILED
    retry = request.deleted_data_summary["retry"]
    assert request.deleted_data_summary == {"retry": retry}
    assert (retry["attempts"], retry["step"], retry["error_class"]) == (
        1,
        "postgres",
        "LockNotAvailable",
    )
    # nothing erased: the account and the (now soft-deleted) workspace stay
    assert await db_session.scalar(select(User.user_id).where(User.user_id == subject)) == subject
    deleted_at = await db_session.scalar(select(Workspace.deleted_at).where(Workspace.id == ws_id))
    assert deleted_at is not None
    shared = (
        await db_session.execute(
            select(Workspace.owner_user_id, Workspace.ownership_epoch).where(
                Workspace.id == shared_id
            )
        )
    ).one()
    assert shared.owner_user_id == subject  # the hand-over was rolled back too
    assert shared.ownership_epoch == 0
    roles = dict(
        (
            await db_session.execute(
                select(WorkspaceMember.user_id, WorkspaceMember.role).where(
                    WorkspaceMember.workspace_id == shared_id
                )
            )
        ).all()
    )
    assert roles == {subject: "owner", successor: "admin"}


# ---------------------------------------------------------------------------
# Owner changes towards the subject while the erasure runs
# ---------------------------------------------------------------------------


async def _member_rows(world, workspace_id, author: str):
    """A context and a memory ``author`` wrote in ``workspace_id``."""
    from models.auth import Context
    from models.memory import Memory

    context = Context(
        id=uuid4(), workspace_id=workspace_id, name=f"ctx-{uuid4().hex[:8]}", created_by=author
    )
    world.db.add(context)
    await world.db.flush()
    memory = Memory(
        id=uuid4(),
        user_id=author,
        workspace_id=workspace_id,
        context_id=context.id,
        summary="a member's memory",
        content="body",
        type="note",
        client="test",
        version=1,
        embedding_status="success",
    )
    world.db.add(memory)
    await world.db.flush()
    return context.id, memory.id


async def _rows_present(db, context_id, memory_id) -> tuple[bool, bool]:
    from models.auth import Context
    from models.memory import Memory

    context = await db.scalar(select(Context.id).where(Context.id == context_id))
    memory = await db.scalar(select(Memory.id).where(Memory.id == memory_id))
    return context is not None, memory is not None


async def _done_or_blocked(maker, task: asyncio.Task, pid_of, timeout: float = 15) -> None:
    """Until ``task`` finished or its backend (``pid_of()``) waits on a lock."""

    async def poll() -> None:
        while not task.done():
            pid = pid_of()
            if pid is not None and await _is_blocked(maker, pid):
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


def _force_erase(maker, user_id: str):
    async def erase():
        async with maker() as db:
            return await AccountErasureService(db).admin_force_erase(
                target_user_id=user_id,
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test",
            )

    return erase()


async def _request_of(db, user_id: str) -> ErasureRequest:
    from services.account_erasure_service import _audit_salt
    from utils.hashing import sha256_hex

    # a complete request carries the pseudonym, a failed one the raw id
    ids = [user_id, sha256_hex(user_id, salt=_audit_salt())]
    return (
        await db.execute(
            select(ErasureRequest)
            .where(ErasureRequest.user_id.in_(ids))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_a_transfer_to_the_subject_waits_for_the_erasure(db_session, connector_world):
    """S hands W1 on to B at step 2; while the run is between steps 2 and 3,
    O transfers W2 to S (an admin there). The transfer waits on ``users[S]``,
    which the run holds until its one commit, and then finds S gone: W2 stays
    O's with M's context and memory."""
    from services.workspace_ownership_service import WorkspaceOwnershipService
    from utils.exceptions import BadRequestError

    world = connector_world
    subject = await world.user("subject")
    successor = await world.user("successor")
    owner = await world.user("owner")
    member = await world.user("member")
    handed_id = (await world.workspace(subject, **{successor: "admin"})).id
    kept_id = (await world.workspace(owner, **{subject: "admin", member: "member"})).id
    context_id, memory_id = await _member_rows(world, kept_id, member)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holding, release = asyncio.Event(), asyncio.Event()
    transfer_pid: int | None = None
    original = AccountErasureService._delete_postgres

    async def paused_step_three(self, target, **kwargs):
        holding.set()
        await asyncio.wait_for(release.wait(), 30)
        return await original(self, target, **kwargs)

    async def transfer():
        nonlocal transfer_pid
        async with maker() as db:
            transfer_pid = await _pid(db)
            return await WorkspaceOwnershipService(db).transfer_ownership(
                workspace_id=kept_id,
                current_owner_id=owner,
                target_user_id=subject,
                performed_by_email="owner@example.test",
            )

    tasks: list[asyncio.Task] = []
    with (
        _external_stores_stubbed(),
        patch.object(AccountErasureService, "_delete_postgres", paused_step_three),
    ):
        try:
            erasing = asyncio.create_task(_force_erase(maker, subject))
            tasks.append(erasing)
            await asyncio.wait_for(holding.wait(), 15)
            transferring = asyncio.create_task(transfer())
            tasks.append(transferring)
            await _done_or_blocked(maker, transferring, lambda: transfer_pid)
            release.set()
            erasure_result, transfer_result = await asyncio.wait_for(
                asyncio.gather(erasing, transferring, return_exceptions=True), 30
            )
        finally:
            release.set()
            await _cancel_all(tasks)

    assert not isinstance(erasure_result, BaseException), erasure_result
    assert erasure_result.status == "complete"
    assert isinstance(transfer_result, BadRequestError), transfer_result
    assert transfer_result.error_code == "WS-OWNER-002"
    kept = await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == kept_id))
    assert kept == owner
    assert await _rows_present(db_session, context_id, memory_id) == (True, True)
    handed = await db_session.scalar(
        select(Workspace.owner_user_id).where(Workspace.id == handed_id)
    )
    assert handed == successor


@pytest.mark.asyncio
async def test_a_workspace_handed_to_the_subject_during_step_one_is_handed_on(
    db_session, connector_world
):
    """Two erasures: A's hands W to S (the lowest-id admin) while S's run is
    in step 1. S's step 2 reads the workspaces S owns under the identity-link
    lock, so it hands W on to B instead of deleting it with S's sole-owned
    ones: W stays, with M's context and memory."""
    world = connector_world
    first = await world.user("handover-a-owner")
    subject = await world.user("handover-b-subject")
    admin = await world.user("handover-c-admin")
    member = await world.user("handover-d-member")
    ws_id = (
        await world.workspace(first, **{subject: "admin", admin: "admin", member: "member"})
    ).id
    context_id, memory_id = await _member_rows(world, ws_id, member)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holding, release = asyncio.Event(), asyncio.Event()
    import services.vector_erasure as vector_module

    original = vector_module.delete_subject_points

    async def paused_step_one(db, user_id: str) -> dict:
        # after the vector pass released its point-writer lock (it serializes
        # the erasures' first steps), before S's step 2
        result = await original(db, user_id)
        if user_id == subject:
            holding.set()
            await asyncio.wait_for(release.wait(), 30)
        return result

    tasks: list[asyncio.Task] = []
    with (
        _external_stores_stubbed(),
        patch.object(vector_module, "delete_subject_points", paused_step_one),
    ):
        try:
            erasing = asyncio.create_task(_force_erase(maker, subject))
            tasks.append(erasing)
            await asyncio.wait_for(holding.wait(), 15)
            # the other erasure runs to its commit while S is in step 1
            first_result = await asyncio.wait_for(_force_erase(maker, first), 30)
            between = await db_session.scalar(
                select(Workspace.owner_user_id)
                .where(Workspace.id == ws_id)
                .execution_options(populate_existing=True)
            )
            release.set()
            erasure_result = await asyncio.wait_for(erasing, 30)
        finally:
            release.set()
            await _cancel_all(tasks)

    assert first_result.status == "complete"
    assert between == subject
    assert erasure_result.status == "complete"
    owner_now = await db_session.scalar(
        select(Workspace.owner_user_id)
        .where(Workspace.id == ws_id)
        .execution_options(populate_existing=True)
    )
    assert owner_now == admin
    assert await _rows_present(db_session, context_id, memory_id) == (True, True)
    request = await _request_of(db_session, subject)
    assert request.deleted_data_summary["workspaces"]["transferred"] == [
        {"workspace_id": str(ws_id), "new_owner_user_id": admin}
    ]


# ---------------------------------------------------------------------------
# Reverse of test_erasure_auto_transfer_rechecks_successor_after_admin_deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_deletion_of_the_successor_waits_for_the_erasure_transfer(
    db_session, connector_world
):
    """Erasure takes the link lock first and hands W to T; the admin
    deletion of T waits on the link lock, then finds T owning W and answers
    409 "Ownership transfer required". No deadlock either way."""
    import services.account_erasure_service as erasure_module

    world = connector_world
    owner = await world.user("erasure-owner")
    successor = await world.user("successor")
    ws = await world.workspace(owner, **{successor: "admin"})
    ws_id = ws.id
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holding, release = asyncio.Event(), asyncio.Event()
    admin_pid: int | None = None
    original = erasure_module.lock_workspace_for_update

    async def paused_lock(db, workspace_id, **kwargs):
        workspace = await original(db, workspace_id, **kwargs)
        holding.set()
        await asyncio.wait_for(release.wait(), 30)
        return workspace

    async def auto_transfer() -> dict:
        async with maker() as db:
            await db.execute(select(User.id).where(User.user_id == owner).with_for_update())
            result = await AccountErasureService(db)._handle_owned_workspaces(owner)
            await db.commit()
            return result

    async def remove() -> None:
        nonlocal admin_pid
        async with maker() as db:
            admin_pid = await _pid(db)
            await delete_user(successor, {"user_id": "operator"}, db)

    tasks: list[asyncio.Task] = []
    with patch.object(erasure_module, "lock_workspace_for_update", paused_lock):
        try:
            transfer = asyncio.create_task(auto_transfer())
            tasks.append(transfer)
            await asyncio.wait_for(holding.wait(), 15)
            deletion = asyncio.create_task(remove())
            tasks.append(deletion)
            await _until_blocked(maker, lambda: admin_pid)
            release.set()
            transfer_result, deletion_result = await asyncio.wait_for(
                asyncio.gather(transfer, deletion, return_exceptions=True), 30
            )
        finally:
            release.set()
            await _cancel_all(tasks)

    assert not isinstance(transfer_result, BaseException), transfer_result
    assert transfer_result["transferred"] == [
        {"workspace_id": str(ws_id), "new_owner_user_id": successor}
    ]
    assert isinstance(deletion_result, HTTPException), deletion_result
    assert deletion_result.status_code == 409
    assert deletion_result.detail["message"] == "Ownership transfer required"
    assert await db_session.scalar(select(User.user_id).where(User.user_id == successor))
    owner_now = await db_session.scalar(
        select(Workspace.owner_user_id).where(Workspace.id == ws_id)
    )
    assert owner_now == successor


# ---------------------------------------------------------------------------
# A short holder is waited out; a workspace the subject does not own
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_two_waits_out_a_short_holder(db_session, connector_world):
    """A memory create keeps its workspace row locked from its quota check
    until it commits. Step 2 tries its NOWAIT lock again for a moment, so a
    holder that commits within it does not cost an erasure attempt."""
    world = connector_world
    subject = await world.user("subject")
    successor = await world.user("successor")
    ws_id = (await world.workspace(subject, **{successor: "admin"})).id
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def step_two() -> dict:
        async with maker() as db:
            await db.execute(select(User.id).where(User.user_id == subject).with_for_update())
            result = await AccountErasureService(db)._handle_owned_workspaces(subject)
            await db.commit()
            return result

    async with maker() as holder:
        await holder.execute(select(Workspace.id).where(Workspace.id == ws_id).with_for_update())
        erasing = asyncio.create_task(step_two())
        await asyncio.sleep(0.15)  # the first tries lose
        assert not erasing.done()
        await holder.commit()
    result = await asyncio.wait_for(erasing, 15)

    assert result["transferred"] == [{"workspace_id": str(ws_id), "new_owner_user_id": successor}]


@pytest.mark.asyncio
async def test_deleting_a_workspace_the_subject_does_not_own_never_deadlocks_the_erasure(
    db_session, connector_world
):
    """Cycle D: O deletes W2, the subject's current workspace, which holds
    the subject's external key. The delete locks the users rows it updates
    right after W2, before the keys, so it never holds a key while it waits on
    ``users[S]``, and the erasure (which deletes the subject's keys while
    holding ``users[S]``) never waits on the delete."""
    from models.auth import ExternalAPIKey

    world = connector_world
    owner = await world.user("owner")
    subject = await world.user("subject")
    ws_id = (await world.workspace(owner, **{subject: "member"})).id
    db_session.add(
        ExternalAPIKey(
            key_name="openai",
            provider="openai",
            encrypted_value="x",
            user_id=subject,
            workspace_id=ws_id,
        )
    )
    await _current_workspace(db_session, subject, ws_id)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def delete_ws() -> None:
        async with maker() as db:
            await WorkspaceService(db).delete_workspace(ws_id, deleted_by=owner)

    async def erase() -> None:
        async with maker() as db:
            await AccountErasureService(db).admin_force_erase(
                target_user_id=subject,
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test",
            )

    tasks: list[asyncio.Task] = []
    with (
        _external_stores_stubbed(),
        _workspace_delete_paused("purge") as (holding, release, holder),
    ):
        try:
            deleting = asyncio.create_task(delete_ws())
            tasks.append(deleting)
            await asyncio.wait_for(holding.wait(), 15)
            erasing = asyncio.create_task(erase())
            tasks.append(erasing)
            await _done_or_waiting_on(maker, erasing, holder)
            release.set()
            erasure_result, delete_result = await asyncio.wait_for(
                asyncio.gather(erasing, deleting, return_exceptions=True), 30
            )
        finally:
            release.set()
            await _cancel_all(tasks)

    assert delete_result is None, delete_result
    assert erasure_result is None, erasure_result
    assert await db_session.scalar(select(User.user_id).where(User.user_id == subject)) is None
    deleted_at = await db_session.scalar(select(Workspace.deleted_at).where(Workspace.id == ws_id))
    assert deleted_at is not None


# ---------------------------------------------------------------------------
# The order itself: a lock trace of every holder
# ---------------------------------------------------------------------------

_ROW_LOCK = re.compile(
    r"\bFOR (UPDATE|NO KEY UPDATE|SHARE|KEY SHARE)\b(?: OF [\w, ]+)?( NOWAIT| SKIP LOCKED)?"
)
_FIRST_FROM = re.compile(r"\bFROM (\w+)")
_DML = re.compile(r"^\s*(UPDATE|DELETE FROM) (\w+)")

# rank of each phase in the global order after the users rows. The users
# rows have rules of their own (``_order_violations``): they come first, except
# in ``delete_workspace``, which locks them right after its workspace row and
# before any row in it (``users`` first would make it wait on the workspace
# while holding users rows).
# ``ws_soft`` is not ranked against the contexts: admin deletion locks the
# contexts the user created (``transfer_user_ownership``) before
# ``delete_user_rows`` locks the soft-deleted workspaces, and that order is
# kept. It is safe: a soft-deleted workspace row is locked only under its
# owner's users row (erasure, admin and CLI deletion) or SKIP LOCKED (the
# deleted-workspace file repair), so two waiters on one always serialize on
# that users row first. It is still ordered after the live ones.
_RANK = {"link": 1, "ws_live": 2, "contexts": 4, "memories": 5}


@dataclass(frozen=True)
class _Lock:
    # users | users_dml | link | ws_live | ws_soft | contexts | memories
    # | other (a locking SELECT of another table) | child (its UPDATE / DELETE)
    kind: str
    nowait: bool
    sql: str


def _locked_table(sql: str, lock_at: int) -> str:
    """The table of the ``FROM`` the locking clause at ``lock_at`` belongs to:
    the closest one before it at the same parenthesis depth (a lock inside a
    sub-select of an UPDATE locks the sub-select's table)."""
    depths, depth = [], 0
    for ch in sql:
        if ch == "(":
            depth += 1
        depths.append(depth)
        if ch == ")":
            depth -= 1
    table = ""
    for match in _FIRST_FROM.finditer(sql, 0, lock_at):
        if depths[match.start()] == depths[lock_at]:
            table = match.group(1)
    return table


def _classify(statement: str, parameters: object) -> _Lock | None:
    sql = " ".join(statement.split())
    if "pg_advisory_xact_lock" in sql:
        if "identity_links" in repr(parameters):
            return _Lock("link", False, sql)
        return None
    dml = _DML.match(sql)
    if dml and dml.group(2) == "users":
        return _Lock("users_dml", False, sql)
    lock = _ROW_LOCK.search(sql)
    if lock is None:
        if dml and dml.group(2) not in ("workspaces", "contexts", "memories"):
            # the rows it writes stay locked: workspace-scoped child rows
            return _Lock("child", False, sql)
        return None
    nowait = lock.group(2) == " NOWAIT"
    if lock.group(2) == " SKIP LOCKED":
        return None  # never waits, never makes anyone wait in a cycle
    name = _locked_table(sql, lock.start())
    if name == "workspaces":
        kind = "ws_soft" if "deleted_at IS NOT NULL" in sql else "ws_live"
    elif name in ("users", "contexts", "memories"):
        kind = name
    else:
        kind = "other"
    return _Lock(kind, nowait, sql)


class _LockTrace:
    """Every lock-taking statement of every connection, cut into transactions
    at each commit / rollback (engine events: ORM flushes are seen too)."""

    def __init__(self, engine) -> None:
        self.engine = engine.sync_engine
        self.open: dict[int, list[_Lock]] = {}
        self.transactions: list[list[_Lock]] = []

    def _statement(self, conn, cursor, statement, parameters, context, executemany):
        lock = _classify(statement, parameters)
        if lock is not None:
            self.open.setdefault(id(conn), []).append(lock)

    def _end(self, conn) -> None:
        locks = self.open.pop(id(conn), [])
        if locks:
            self.transactions.append(locks)

    def __enter__(self) -> _LockTrace:
        event.listen(self.engine, "before_cursor_execute", self._statement)
        event.listen(self.engine, "commit", self._end)
        event.listen(self.engine, "rollback", self._end)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._statement)
        event.remove(self.engine, "commit", self._end)
        event.remove(self.engine, "rollback", self._end)
        for conn in list(self.open):
            self._end_id(conn)

    def _end_id(self, key: int) -> None:
        locks = self.open.pop(key, [])
        if locks:
            self.transactions.append(locks)

    def kinds(self) -> list[list[tuple[str, bool]]]:
        return [[(lock.kind, lock.nowait) for lock in tx] for tx in self.transactions]


def _order_violations(transaction: list[_Lock]) -> list[str]:
    """The global order's rules, for one transaction."""
    problems: list[str] = []
    holds_user = False
    user_before_link = False
    linked = False
    reached = -1
    seen: set[str] = set()
    for lock in transaction:
        if lock.kind in ("users", "users_dml"):
            if linked and not user_before_link:
                problems.append(f"users lock after the link lock: {lock.sql[:160]}")
            if not holds_user and seen & {"child", "other", "contexts", "memories"}:
                # ``delete_workspace``: after its workspace row, before its rows
                problems.append(f"users lock after a workspace-scoped row: {lock.sql[:160]}")
            holds_user = True
            if not linked:
                user_before_link = True
        if lock.kind == "link":
            linked = True
        if lock.kind == "ws_live" and holds_user and not lock.nowait:
            problems.append(f"waits on a live workspace holding a users row: {lock.sql[:160]}")
        if lock.kind == "ws_live" and "ws_soft" in seen and "ws_live" not in seen:
            problems.append(f"live workspace after a soft-deleted one: {lock.sql[:160]}")
        rank = _RANK.get(lock.kind)
        # only a phase's first lock is ordered: a later one re-takes rows the
        # transaction holds (a lock last until the commit, so that is free)
        if rank is not None and lock.kind not in seen:
            if rank < reached:
                problems.append(f"{lock.kind} after a later phase: {lock.sql[:160]}")
            reached = max(reached, rank)
        seen.add(lock.kind)
    return problems


def _assert_global_order(trace: _LockTrace) -> None:
    problems = [p for tx in trace.transactions for p in _order_violations(tx)]
    assert problems == [], "\n".join(problems)


def test_the_rules_catch_each_inversion():
    """The checker itself: each rule fires on the inversion it is for."""

    def tx(*kinds: tuple[str, bool]) -> list[_Lock]:
        return [_Lock(kind, nowait, kind) for kind, nowait in kinds]

    assert _order_violations(tx(("users", False), ("link", False), ("ws_live", True))) == []
    assert _order_violations(tx(("users", False), ("ws_live", False)))  # waits holding users
    assert _order_violations(tx(("link", False), ("users", False)))  # users after link
    assert _order_violations(tx(("ws_live", True), ("link", False)))  # link after workspaces
    assert _order_violations(tx(("memories", False), ("contexts", False)))
    assert _order_violations(tx(("ws_soft", False), ("ws_live", True)))
    # a soft-deleted workspace may wait, and after the contexts (admin deletion)
    assert _order_violations(tx(("users", False), ("contexts", False), ("ws_soft", False))) == []
    # a re-lock of a users row the transaction already holds is fine
    assert _order_violations(tx(("users", False), ("link", False), ("users", False))) == []
    # delete_workspace: the workspace row, the users rows, then its rows
    assert (
        _order_violations(
            tx(("ws_live", False), ("users", False), ("child", False), ("users_dml", False))
        )
        == []
    )
    # ... not its rows first: it would hold them while waiting on a users row
    assert _order_violations(tx(("ws_live", False), ("child", False), ("users_dml", False)))


@pytest.mark.asyncio
async def test_the_erasure_steps_take_the_global_order(db_session, connector_world):
    """Steps 2 and 3 of a force-erase: a shared workspace handed on, a
    sole-owned and a soft-deleted one hard-deleted."""
    world = connector_world
    subject = await world.user("subject")
    successor = await world.user("successor")
    shared_id = (await world.workspace(subject, **{successor: "admin"})).id
    await world.workspace(subject)
    gone_id = (await world.workspace(subject)).id
    await db_session.execute(
        update(Workspace).where(Workspace.id == gone_id).values(deleted_at=func.now())
    )
    await _current_workspace(db_session, subject, shared_id)
    await db_session.commit()

    with _external_stores_stubbed(), _LockTrace(db_session.bind) as trace:
        await world.erase(subject)

    _assert_global_order(trace)
    kinds = trace.kinds()
    # step 2: users -> link -> the shared workspace NOWAIT
    assert any(
        tx[:1] == [("users", False)] and ("link", False) in tx and ("ws_live", True) in tx
        for tx in kinds
    ), kinds
    # step 3: the owned workspaces before the contexts, soft-deleted after live
    step3 = [tx for tx in kinds if ("ws_soft", False) in tx]
    assert step3, kinds
    phases = [k for k, _ in step3[0] if k in ("ws_live", "ws_soft", "contexts")]
    assert phases.index("ws_live") < phases.index("ws_soft") < phases.index("contexts")
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == shared_id))
        == successor
    )


@pytest.mark.asyncio
async def test_admin_user_deletion_takes_the_global_order(db_session, connector_world):
    from api.routes.admin import UserDeleteRequest

    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    shared = await world.workspace(target, **{successor: "admin"})
    gone = await world.workspace(target)
    await db_session.execute(
        update(Workspace).where(Workspace.id == gone.id).values(deleted_at=func.now())
    )
    await db_session.commit()

    with _LockTrace(db_session.bind) as trace:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(workspace_transfers={shared.id: successor}),
        )

    _assert_global_order(trace)
    assert any(("ws_soft", False) in tx and ("ws_live", True) in tx for tx in trace.kinds())


@pytest.mark.asyncio
async def test_cli_admin_deletion_takes_the_global_order(
    db_session,
    connector_world,
    sole_password_admin,  # noqa: F811 (the fixture imported above)
    transfer_audits,  # noqa: F811
):
    from cli import delete_admin

    world = connector_world
    admin_id = await _password_admin(db_session, world)
    successor = await world.user("successor")
    shared = await world.workspace(admin_id, **{successor: "admin"})
    await world.workspace(admin_id)
    transfer_audits.append(shared.id)
    await db_session.commit()

    with _LockTrace(db_session.bind) as trace:
        code = await delete_admin.delete_password_admin(
            db_session, transfer_to=successor, ask=lambda _: True
        )
    assert code == 0
    _assert_global_order(trace)


@pytest.mark.asyncio
async def test_linking_member_removal_and_workspace_delete_take_the_global_order(
    db_session, connector_world
):
    from services.identity_link_service import IdentityLinkService

    world = connector_world
    owner = await world.user("owner")
    member = await world.user("member")
    other = await world.user("other")
    ws = await world.workspace(owner, **{member: "member"})
    await _current_workspace(db_session, owner, ws.id)
    await db_session.commit()

    with _LockTrace(db_session.bind) as trace:
        await IdentityLinkService(db_session).link(member, other)
        await db_session.commit()
        await WorkspaceService(db_session).remove_member(ws.id, member)
        await WorkspaceService(db_session).delete_workspace(ws.id, deleted_by=owner)

    _assert_global_order(trace)
    kinds = trace.kinds()
    assert [("users", False), ("link", False)] == [
        k for k in kinds[0] if k[0] in ("users", "link")
    ][:2]
    # delete_workspace: its workspace row, then the users whose preference it
    # clears, before any of its rows
    assert any(tx[:2] == [("ws_live", False), ("users", False)] for tx in kinds), kinds
