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
A referral redemption holds the referrer's ``users`` row from its cap check,
so it takes workspace rows ``NOWAIT`` too and answers 409 when one stays
busy (#2443).
Each test drives two real Postgres sessions and waits on
``pg_blocking_pids`` before releasing the paused one.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes.admin import delete_user
from config.settings import get_settings
from models.auth import User, Workspace, WorkspaceInvitation, WorkspaceMember
from models.erasure import (
    REASON_USER_REQUEST_VIA_SUPPORT,
    STATUS_COOLING_OFF,
    STATUS_FAILED,
    ErasureRequest,
)
from models.referral import ReferralGrant
from services.account_erasure_service import AccountErasureService
from services.file_storage_service import FileStorageService
from services.invitation_service import InvitationService
from services.referral_service import ReferralService
from services.workspace_locks import NOWAIT_LOCK_BACKOFF_SECONDS
from services.workspace_service import WorkspaceService
from utils.exceptions import (
    ConflictError,
    ErasureRetryScheduledError,
    NotFoundException,
    ValidationError,
)

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
# Cycle C: a sole-owned workspace against the same workspace delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sole_owned_workspace_its_delete_holds_reschedules(db_session, connector_world):
    """A sole-owned W: nothing to hand on, and step 3's deletes of W's rows
    would wait on the delete that holds W while erasure holds ``users[S]``.
    Step 2 takes every live workspace S owns ``NOWAIT`` before it reads their
    members (#2455; until then step 3 was the first to lock W), so the
    request is rescheduled at step 2; nothing is erased and the delete
    completes. The shared workspace is not handed on: the step stops before
    any hand-over, and steps 2 and 3 are one transaction, so none is left
    behind."""
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
        "workspaces",
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
    assert shared.owner_user_id == subject  # never handed on: the step stopped first
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


@pytest.mark.asyncio
async def test_a_lock_conflict_at_step_three_rolls_back_the_hand_over(db_session, connector_world):
    """Steps 2 and 3 are one transaction (v0.134.2). Step 2 holds every live
    workspace the subject owns (#2455), so the lock conflict step 3 can still
    meet is a deadlock it is the victim of (40P01, injected here once the
    hand-over is in the transaction): the hand-over is rolled back with the
    deletes and the request is rescheduled at ``postgres``."""
    from services.account_erasure_service import DEADLOCK_ERROR_CLASS, _LockConflict

    world = connector_world
    subject = await world.user("subject")
    successor = await world.user("successor")
    shared_id = (await world.workspace(subject, **{successor: "admin"})).id
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    owner_in_step_three: list[str | None] = []

    async def deadlocked(self, target):
        owner_in_step_three.append(
            await self.db.scalar(select(Workspace.owner_user_id).where(Workspace.id == shared_id))
        )
        raise _LockConflict(DEADLOCK_ERROR_CLASS)

    with (
        _external_stores_stubbed(),
        patch.object(AccountErasureService, "_pseudonymize_audit_logs", deadlocked),
        pytest.raises(ErasureRetryScheduledError),
    ):
        await asyncio.wait_for(_force_erase(maker, subject), 30)

    assert owner_in_step_three == [successor]  # handed on, not yet committed
    request = await _request_of(db_session, subject)
    assert request.status == STATUS_COOLING_OFF
    retry = request.deleted_data_summary["retry"]
    assert request.deleted_data_summary == {"retry": retry}
    assert (retry["attempts"], retry["step"], retry["error_class"]) == (
        1,
        "postgres",
        DEADLOCK_ERROR_CLASS,
    )
    assert await db_session.scalar(select(User.user_id).where(User.user_id == subject)) == subject
    shared = (
        await db_session.execute(
            select(Workspace.owner_user_id, Workspace.ownership_epoch)
            .where(Workspace.id == shared_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    assert (shared.owner_user_id, shared.ownership_epoch) == (subject, 0)  # rolled back
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


@pytest.mark.asyncio
async def test_a_member_added_to_a_sole_owned_workspace_waits_for_the_erasure(
    db_session, connector_world
):
    """#2455: step 2 finds no other member in W, so step 3 hard-deletes it.
    Step 2 locks the live workspaces the subject owns before it reads their
    members, so an ``add_member`` (``FOR UPDATE`` on W) while the run is
    between steps 2 and 3 waits for the run's commit and then finds W gone:
    the newcomer's membership is never deleted with W."""
    world = connector_world
    subject = await world.user("subject")
    newcomer = await world.user("newcomer")
    ws_id = (await world.workspace(subject)).id
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holding, release = asyncio.Event(), asyncio.Event()
    add_pid: int | None = None
    original = AccountErasureService._delete_postgres

    async def paused_step_three(self, target, **kwargs):
        holding.set()
        await asyncio.wait_for(release.wait(), 30)
        return await original(self, target, **kwargs)

    async def add() -> None:
        nonlocal add_pid
        async with maker() as db:
            add_pid = await _pid(db)
            await WorkspaceService(db).add_member(ws_id, newcomer, invited_by=subject)

    tasks: list[asyncio.Task] = []
    with (
        _external_stores_stubbed(),
        patch.object(AccountErasureService, "_delete_postgres", paused_step_three),
    ):
        try:
            erasing = asyncio.create_task(_force_erase(maker, subject))
            tasks.append(erasing)
            await asyncio.wait_for(holding.wait(), 15)
            adding = asyncio.create_task(add())
            tasks.append(adding)
            await _done_or_blocked(maker, adding, lambda: add_pid)
            # before #2455 nothing held W here: the add committed at once
            added_between_the_steps = adding.done()
            release.set()
            erasure_result, add_result = await asyncio.wait_for(
                asyncio.gather(erasing, adding, return_exceptions=True), 30
            )
        finally:
            release.set()
            await _cancel_all(tasks)

    assert not added_between_the_steps, add_result
    assert not isinstance(erasure_result, BaseException), erasure_result
    assert erasure_result.status == "complete"
    assert isinstance(add_result, NotFoundException), add_result
    assert await db_session.scalar(select(Workspace.id).where(Workspace.id == ws_id)) is None
    members = await db_session.scalar(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == ws_id)
    )
    assert members == 0
    request = await _request_of(db_session, subject)
    assert request.deleted_data_summary["workspaces"] == {
        "transferred": [],
        "sole_owner_workspaces": 1,
    }


async def _pending_invitation(db, workspace_id, invited_by: str, email: str) -> str:
    """A pending member invitation to ``workspace_id`` for ``email``; its token."""
    token = uuid4().hex + uuid4().hex
    db.add(
        WorkspaceInvitation(
            workspace_id=workspace_id,
            token=token,
            email=email,
            role="member",
            invited_by=invited_by,
        )
    )
    await db.flush()
    return token


async def _accept_left_nothing(db, workspace_id, token: str, user_id: str, current_id) -> None:
    """The failed accept wrote nothing: no membership of the accepter in the
    workspace, the invitation not accepted (or gone with its workspace), and
    the accepter's current workspace unchanged."""
    members = await db.scalar(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id)
    )
    accepted_by = await db.scalar(
        select(WorkspaceInvitation.accepted_by).where(WorkspaceInvitation.token == token)
    )
    current = await db.scalar(select(User.current_workspace_id).where(User.user_id == user_id))
    assert (members, accepted_by, current) == (0, None, current_id)


@pytest.mark.asyncio
async def test_an_invitation_accepted_into_a_sole_owned_workspace_waits_for_the_erasure(
    db_session, connector_world
):
    """#2564: the accept path of the #2455 race. Step 2 holds W, which the
    subject owns alone, so step 3 hard-deletes it. An ``accept_invitation``
    to W while the run is between steps 2 and 3 waits on W's row lock and then
    finds W gone: a 404, not the foreign-key violation (500) its member INSERT
    used to end in."""
    world = connector_world
    subject = await world.user("subject")
    newcomer = await world.user("newcomer")
    ws_id = (await world.workspace(subject)).id
    own_id = (await world.workspace(newcomer)).id
    await _current_workspace(db_session, newcomer, own_id)
    email = f"{newcomer}@example.com"
    token = await _pending_invitation(db_session, ws_id, subject, email)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holding, release = asyncio.Event(), asyncio.Event()
    accept_pid: int | None = None
    original = AccountErasureService._delete_postgres

    async def paused_step_three(self, target, **kwargs):
        holding.set()
        await asyncio.wait_for(release.wait(), 30)
        return await original(self, target, **kwargs)

    async def accept() -> None:
        nonlocal accept_pid
        async with maker() as db:
            accept_pid = await _pid(db)
            await InvitationService(db).accept_invitation(
                token=token, user_id=newcomer, user_email=email
            )
            await db.commit()

    tasks: list[asyncio.Task] = []
    with (
        _external_stores_stubbed(),
        patch.object(AccountErasureService, "_delete_postgres", paused_step_three),
        _LockTrace(db_session.bind) as trace,
    ):
        try:
            erasing = asyncio.create_task(_force_erase(maker, subject))
            tasks.append(erasing)
            await asyncio.wait_for(holding.wait(), 15)
            accepting = asyncio.create_task(accept())
            tasks.append(accepting)
            await _done_or_blocked(maker, accepting, lambda: accept_pid)
            accepted_between_the_steps = accepting.done()
            release.set()
            erasure_result, accept_result = await asyncio.wait_for(
                asyncio.gather(erasing, accepting, return_exceptions=True), 30
            )
        finally:
            release.set()
            await _cancel_all(tasks)

    _assert_global_order(trace)  # #2601
    assert not accepted_between_the_steps, accept_result
    assert not isinstance(erasure_result, BaseException), erasure_result
    assert erasure_result.status == "complete"
    assert isinstance(accept_result, NotFoundException), accept_result
    assert await db_session.scalar(select(Workspace.id).where(Workspace.id == ws_id)) is None
    await _accept_left_nothing(db_session, ws_id, token, newcomer, own_id)


@pytest.mark.asyncio
async def test_an_invitation_accepted_into_a_workspace_deleted_meanwhile_is_a_404(
    db_session, connector_world
):
    """#2564: a holder has W's row; the accept waits on it. The holder
    soft-deletes W and commits: the accept's lock re-reads the row, finds it
    deleted and answers 404. Before, the accept read W unlocked and joined
    the deleted workspace."""
    world = connector_world
    owner = await world.user("owner")
    newcomer = await world.user("newcomer")
    ws_id = (await world.workspace(owner)).id
    email = f"{newcomer}@example.com"
    token = await _pending_invitation(db_session, ws_id, owner, email)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    accept_pid: int | None = None

    async def accept() -> None:
        nonlocal accept_pid
        async with maker() as db:
            accept_pid = await _pid(db)
            await InvitationService(db).accept_invitation(
                token=token, user_id=newcomer, user_email=email
            )
            await db.commit()

    tasks: list[asyncio.Task] = []
    with _LockTrace(db_session.bind) as trace:
        async with maker() as holder:
            try:
                await holder.execute(
                    select(Workspace.id).where(Workspace.id == ws_id).with_for_update()
                )
                accepting = asyncio.create_task(accept())
                tasks.append(accepting)
                await _done_or_blocked(maker, accepting, lambda: accept_pid)
                accepted_while_held = accepting.done()
                await holder.execute(
                    update(Workspace).where(Workspace.id == ws_id).values(deleted_at=func.now())
                )
                await holder.commit()
                (accept_result,) = await asyncio.wait_for(
                    asyncio.gather(accepting, return_exceptions=True), 30
                )
            finally:
                await holder.rollback()
                await _cancel_all(tasks)

    _assert_global_order(trace)  # #2601
    assert not accepted_while_held, accept_result
    assert isinstance(accept_result, NotFoundException), accept_result
    await _accept_left_nothing(db_session, ws_id, token, newcomer, None)


@pytest.mark.asyncio
async def test_two_accepts_of_one_invitation_admit_one_user(db_session, connector_world):
    """#2564: two users accept one invitation with no email restriction and
    both wait on W's row lock. The one that gets it second re-reads the
    invitation, finds it accepted and answers 400. Before, it went on with the
    invitation it had read before the wait and joined too."""
    world = connector_world
    owner = await world.user("owner")
    first = await world.user("first")
    second = await world.user("second")
    ws_id = (await world.workspace(owner)).id
    token = uuid4().hex + uuid4().hex
    db_session.add(
        WorkspaceInvitation(
            workspace_id=ws_id, token=token, email=None, role="member", invited_by=owner
        )
    )
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    pids: dict[str, int] = {}

    async def accept(user_id: str) -> None:
        async with maker() as db:
            pids[user_id] = await _pid(db)
            await InvitationService(db).accept_invitation(
                token=token, user_id=user_id, user_email=f"{user_id}@example.com"
            )
            await db.commit()

    tasks: list[asyncio.Task] = []
    with _LockTrace(db_session.bind) as trace:
        async with maker() as holder:
            try:
                await holder.execute(
                    select(Workspace.id).where(Workspace.id == ws_id).with_for_update()
                )
                for user_id in (first, second):
                    task = asyncio.create_task(accept(user_id))
                    tasks.append(task)
                    await _done_or_blocked(maker, task, lambda u=user_id: pids.get(u))
                accepted_while_held = [task.done() for task in tasks]
                await holder.commit()
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 30)
            finally:
                await holder.rollback()
                await _cancel_all(tasks)

    # #2601: the winner's accept ran to its writes: workspace, users row, rows
    _assert_global_order(trace)
    assert accepted_while_held == [False, False], results
    winners = [u for u, r in zip((first, second), results, strict=True) if r is None]
    losers = [r for r in results if r is not None]
    assert len(winners) == 1 and len(losers) == 1, results
    assert isinstance(losers[0], ValidationError), losers[0]
    joined = await db_session.scalars(
        select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == ws_id, WorkspaceMember.user_id.in_([first, second])
        )
    )
    accepted_by = await db_session.scalar(
        select(WorkspaceInvitation.accepted_by).where(WorkspaceInvitation.token == token)
    )
    assert (list(joined), accepted_by) == (winners, winners[0])


def _first_of_each(kinds: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """``kinds`` without the re-takes: each phase where it is first taken."""
    return list(dict.fromkeys(kinds))


@pytest.mark.asyncio
async def test_an_accept_locks_the_accepters_users_row_before_its_rows(db_session, connector_world):
    """#2601: an accept takes the workspace row, then the accepter's
    ``users`` row (its ``current_workspace_id`` is set), and only then writes
    (the invitation UPDATE, the ``users`` UPDATE, in the flush's order): the
    ``delete_workspace`` order. Before, its first lock on the ``users`` row
    was that UPDATE, flushed with the invitation's."""
    world = connector_world
    owner = await world.user("owner")
    newcomer = await world.user("newcomer")
    ws_id = (await world.workspace(owner)).id
    email = f"{newcomer}@example.com"
    token = await _pending_invitation(db_session, ws_id, owner, email)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    with _LockTrace(db_session.bind) as trace:
        async with maker() as db:
            await InvitationService(db).accept_invitation(
                token=token, user_id=newcomer, user_email=email
            )
            await db.commit()

    _assert_global_order(trace)
    (accept,) = trace.kinds()
    phases = _first_of_each(accept)
    # the users lock waits: only the workspace row is held then
    assert phases[:2] == [("ws_live", False), ("users", False)], accept
    assert set(phases[2:]) == {("child", False), ("users_dml", False)}, accept
    current = await db_session.scalar(
        select(User.current_workspace_id)
        .where(User.user_id == newcomer)
        .execution_options(populate_existing=True)
    )
    assert current == ws_id


async def _lock_wait_of(maker, pid: int) -> tuple[list[int], str]:
    """The backends ``pid`` waits on, and the statement it waits in."""
    async with maker() as observer:
        blocking, query = (
            await observer.execute(
                text("SELECT pg_blocking_pids(:pid), query FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
        ).one()
    return list(blocking), " ".join(query.split())


async def _accept_during_the_accepters_erasure(
    db_session, world, *, step_three_fails: bool
) -> SimpleNamespace:
    """#2601: N's erasure stops between its steps 2 and 3, holding
    ``users[N]``; N accepts an invitation to O's W, addressed to N's email
    (step 3 deletes it). Then the erasure goes on: it finishes, or with
    ``step_three_fails`` it fails at step 3 and rolls back."""
    owner = await world.user("owner")
    newcomer = await world.user("newcomer")
    ws_id = (await world.workspace(owner)).id
    own_id = (await world.workspace(newcomer)).id
    await _current_workspace(db_session, newcomer, own_id)
    email = f"{newcomer}@example.com"
    token = await _pending_invitation(db_session, ws_id, owner, email)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holding, release = asyncio.Event(), asyncio.Event()
    pids: dict[str, int] = {}
    original = AccountErasureService._delete_postgres

    async def paused_step_three(self, target, **kwargs):
        pids["erasure"] = await _pid(self.db)
        holding.set()
        await asyncio.wait_for(release.wait(), 30)
        if step_three_fails:
            raise RuntimeError("step 3 failed")
        return await original(self, target, **kwargs)

    async def accept() -> None:
        async with maker() as db:
            pids["accept"] = await _pid(db)
            await InvitationService(db).accept_invitation(
                token=token, user_id=newcomer, user_email=email
            )
            await db.commit()

    tasks: list[asyncio.Task] = []
    waited: tuple[list[int], str] | None = None
    with (
        _external_stores_stubbed(),
        patch.object(AccountErasureService, "_delete_postgres", paused_step_three),
        _LockTrace(db_session.bind) as trace,
    ):
        try:
            erasing = asyncio.create_task(_force_erase(maker, newcomer))
            tasks.append(erasing)
            await asyncio.wait_for(holding.wait(), 15)
            accepting = asyncio.create_task(accept())
            tasks.append(accepting)
            await _done_or_blocked(maker, accepting, lambda: pids.get("accept"))
            if not accepting.done():
                waited = await _lock_wait_of(maker, pids["accept"])
            release.set()
            erasure_result, accept_result = await asyncio.wait_for(
                asyncio.gather(erasing, accepting, return_exceptions=True), 30
            )
        finally:
            release.set()
            await _cancel_all(tasks)

    return SimpleNamespace(
        newcomer=newcomer,
        ws_id=ws_id,
        token=token,
        erasure_pid=pids["erasure"],
        waited=waited,
        erasure_result=erasure_result,
        accept_result=accept_result,
        trace=trace,
    )


def _waited_on_the_users_row(run: SimpleNamespace) -> None:
    """The accept waited on the erasure, in its ``users`` row lock."""
    assert run.waited is not None, run.accept_result  # it did not finish first
    blocking, query = run.waited
    assert blocking == [run.erasure_pid], (blocking, query)
    assert query.startswith("SELECT users.") and query.endswith("FOR NO KEY UPDATE"), query


@pytest.mark.asyncio
async def test_an_accept_waits_out_the_accepters_erasure_and_is_a_404(db_session, connector_world):
    """#2601: N's erasure holds ``users[N]`` between its steps 2 and 3; N's
    accept takes W, then waits on ``users[N]`` before it writes any row. Step
    3 deletes N's invitations, the one the accept read among them, without
    waiting, and commits; the accept finds N gone and answers 404. Before, the
    accept held the invitation it had marked accepted while it waited on
    ``users[N]`` for its ``current_workspace_id``, and step 3 waited on that
    invitation: ``deadlock detected``, one side killed."""
    run = await _accept_during_the_accepters_erasure(
        db_session, connector_world, step_three_fails=False
    )

    _waited_on_the_users_row(run)
    _assert_global_order(run.trace)
    assert not isinstance(run.erasure_result, BaseException), run.erasure_result
    assert run.erasure_result.status == "complete"
    assert isinstance(run.accept_result, NotFoundException), run.accept_result
    assert str(run.accept_result) == f"User not found: {run.newcomer}"
    assert await db_session.scalar(select(User.user_id).where(User.user_id == run.newcomer)) is None
    # gone with N's other invitations (step 3), never accepted
    assert (
        await db_session.scalar(
            select(WorkspaceInvitation.id).where(WorkspaceInvitation.token == run.token)
        )
        is None
    )
    await _accept_left_nothing(db_session, run.ws_id, run.token, run.newcomer, None)


@pytest.mark.asyncio
async def test_an_accept_waits_out_a_rolled_back_erasure_of_the_accepter_and_joins(
    db_session, connector_world
):
    """#2601: as above, but step 3 fails and the erasure rolls back: the
    accept gets ``users[N]`` and joins W, its current workspace W."""
    run = await _accept_during_the_accepters_erasure(
        db_session, connector_world, step_three_fails=True
    )

    _waited_on_the_users_row(run)
    _assert_global_order(run.trace)
    assert isinstance(run.erasure_result, RuntimeError), run.erasure_result
    assert run.accept_result is None, run.accept_result
    request = await _request_of(db_session, run.newcomer)
    assert request.status == STATUS_FAILED
    member_role = await db_session.scalar(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == run.ws_id, WorkspaceMember.user_id == run.newcomer
        )
    )
    accepted_by = await db_session.scalar(
        select(WorkspaceInvitation.accepted_by).where(WorkspaceInvitation.token == run.token)
    )
    current = await db_session.scalar(
        select(User.current_workspace_id)
        .where(User.user_id == run.newcomer)
        .execution_options(populate_existing=True)
    )
    assert (member_role, accepted_by, current) == ("member", run.newcomer, run.ws_id)


@pytest.mark.asyncio
async def test_an_accept_does_not_wait_on_the_accepters_open_remember(db_session, connector_world):
    """#2601: the accept locks ``users[N]`` ``FOR NO KEY UPDATE``, which the
    ``FOR KEY SHARE`` a remember by N holds on it to its commit
    (``hold_remember_author``) does not block. N, a member of W, has a
    remember into W open and accepts an invitation to W: the accept answers
    "already a member" at once, then the remember takes W for its quota
    check. With ``FOR UPDATE`` the accept held W and waited on the remember,
    which then waited on W: ``deadlock detected``."""
    from services.erasure_write_guard import hold_remember_author

    world = connector_world
    owner = await world.user("owner")
    newcomer = await world.user("newcomer")
    ws_id = (await world.workspace(owner, **{newcomer: "member"})).id
    email = f"{newcomer}@example.com"
    token = await _pending_invitation(db_session, ws_id, owner, email)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    accept_pid: int | None = None

    async def accept() -> None:
        nonlocal accept_pid
        async with maker() as db:
            accept_pid = await _pid(db)
            await InvitationService(db).accept_invitation(
                token=token, user_id=newcomer, user_email=email
            )
            await db.commit()

    tasks: list[asyncio.Task] = []
    async with maker() as remember:
        try:
            await hold_remember_author(remember, newcomer)
            accepting = asyncio.create_task(accept())
            tasks.append(accepting)
            await _done_or_blocked(maker, accepting, lambda: accept_pid)
            accepted_at_once = accepting.done()
            # the remember's quota check (``check_memory_quota``) locks W
            (quota_lock,) = await asyncio.wait_for(
                asyncio.gather(
                    remember.execute(
                        select(Workspace.id).where(Workspace.id == ws_id).with_for_update()
                    ),
                    return_exceptions=True,
                ),
                30,
            )
            await remember.rollback()
            (accept_result,) = await asyncio.wait_for(
                asyncio.gather(accepting, return_exceptions=True), 30
            )
        finally:
            await remember.rollback()
            await _cancel_all(tasks)

    assert accepted_at_once, accept_result
    assert not isinstance(quota_lock, BaseException), quota_lock
    assert isinstance(accept_result, ValidationError), accept_result
    assert str(accept_result) == "You are already a member of this workspace"


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
async def test_step_two_locks_every_live_owned_workspace_before_the_members(
    db_session, connector_world
):
    """#2455: one shared and one sole-owned workspace. Right after the link
    lock, step 2 takes every live workspace the subject owns ``NOWAIT`` in
    one statement, before any other row; the shared one's re-lock for its
    hand-over comes after it."""
    world = connector_world
    subject = await world.user("subject")
    successor = await world.user("successor")
    shared_id = (await world.workspace(subject, **{successor: "admin"})).id
    sole_id = (await world.workspace(subject)).id
    await db_session.commit()

    with _external_stores_stubbed(), _LockTrace(db_session.bind) as trace:
        await world.erase(subject)

    _assert_global_order(trace)
    # steps 2 and 3: one transaction, the one that locks the soft-deleted rows
    (steps,) = [tx for tx in trace.transactions if any(lock.kind == "ws_soft" for lock in tx)]
    kinds = [(lock.kind, lock.nowait) for lock in steps]
    first_ws = next(i for i, lock in enumerate(steps) if lock.kind == "ws_live")
    assert kinds[first_ws - 1] == ("link", False), kinds
    assert not {kind for kind, _ in kinds[:first_ws]} & {"other", "child"}, kinds
    owned = steps[first_ws]
    assert owned.nowait, owned.sql
    assert "WHERE workspaces.owner_user_id = " in owned.sql, owned.sql
    assert "workspaces.deleted_at IS NULL" in owned.sql, owned.sql
    relock = next(
        i
        for i, lock in enumerate(steps)
        if lock.kind == "ws_live" and "WHERE workspaces.id = " in lock.sql
    )
    assert first_ws < relock, kinds
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == shared_id))
        == successor
    )
    assert await db_session.scalar(select(Workspace.id).where(Workspace.id == sole_id)) is None


@pytest.mark.asyncio
async def test_the_hand_over_and_the_connector_target_re_own_take_the_global_order(
    db_session, connector_world
):
    """#2422: an erasure whose identity-link hand-over passes one private
    context on and leaves a connector's write target, which then goes to the
    workspace's owner: the write-target lookups only read, and the re-own
    re-takes contexts the erasure holds."""
    from models.auth import Context, IdentityLink
    from models.memory import Memory
    from models.resource import WorkspaceConnector

    world = connector_world
    subject = await world.user("subject")
    linked = await world.user("linked")
    owner = await world.user("owner")
    ws = await world.workspace(owner, **{subject: "admin", linked: "admin"})
    connector_id = (await world.connector(ws, by=subject)).connector.id
    contexts = [
        Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"priv_{uuid4().hex[:8]}",
            created_by=subject,
            is_private=True,
        )
        for _ in range(2)
    ]
    db_session.add_all(contexts)
    await db_session.flush()
    target_id, sibling_id = contexts[0].id, contexts[1].id
    await db_session.execute(
        update(WorkspaceConnector)
        .where(WorkspaceConnector.id == connector_id)
        .values(context_id=target_id)
    )
    group = uuid4()
    db_session.add_all(
        [
            IdentityLink(group_id=group, user_id=subject, linked_by=subject),
            IdentityLink(group_id=group, user_id=linked, linked_by=subject),
            *(
                Memory(
                    id=uuid4(),
                    user_id=linked,
                    workspace_id=ws.id,
                    context_id=context_id,
                    summary="a summary long enough",
                    content="content",
                    type="note",
                    client="pytest",
                )
                for context_id in (target_id, sibling_id)
            ),
        ]
    )
    await db_session.commit()

    with _external_stores_stubbed(), _LockTrace(db_session.bind) as trace:
        counts = await world.erase(subject)

    _assert_global_order(trace)
    assert counts["contexts_handed_over"] == 1
    assert counts["connector_contexts_reowned"] == 1
    owners = dict(
        (
            await db_session.execute(
                select(Context.id, Context.created_by).where(
                    Context.id.in_([target_id, sibling_id])
                )
            )
        ).all()
    )
    assert owners == {target_id: owner, sibling_id: linked}


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


def _written_table(sql: str) -> str:
    """The table a traced statement writes or locks rows of."""
    dml = _DML.match(sql)
    if dml:
        return dml.group(2)
    lock = _ROW_LOCK.search(sql)
    return _locked_table(sql, lock.start()) if lock else ""


def _agents_before_keys(trace: _LockTrace) -> None:
    """In the transaction that settles the departing user's agents (#2373),
    the agent rows are locked before any ``api_keys`` row, the order
    ``verify_key`` takes (the agent's ``last_seen_at``, then the key's)."""
    settling = [
        tx
        for tx in trace.transactions
        if any(lock.sql.startswith("SELECT agents_1.id") for lock in tx)
    ]
    assert len(settling) == 1, trace.kinds()
    sqls = [lock.sql for lock in settling[0]]
    agents = next(i for i, sql in enumerate(sqls) if sql.startswith("SELECT agents_1.id"))
    assert "FOR NO KEY UPDATE" in sqls[agents]
    keys = [i for i, sql in enumerate(sqls) if _written_table(sql) == "api_keys"]
    assert keys and agents < min(keys), sqls


async def _agent_world(world, subject: str):
    """A shared workspace where ``subject`` registered one agent and holds the
    only key of another, so both agent updates and the key delete run."""
    owner = await world.user("owner")
    shared = await world.workspace(owner, **{subject: "admin"})
    registered = await world.agent(shared, by=subject)
    served = await world.agent(shared, by=owner)
    await world.agent_key(subject, shared, served)
    return owner, registered.id, served.id


@pytest.mark.asyncio
async def test_an_erasure_that_settles_agents_takes_the_global_order(db_session, connector_world):
    world = connector_world
    subject = await world.user("subject")
    owner, registered_id, served_id = await _agent_world(world, subject)
    await world.workspace(subject)  # sole-owned: deleted with its agents
    await db_session.commit()

    with _external_stores_stubbed(), _LockTrace(db_session.bind) as trace:
        counts = await world.erase(subject)

    _assert_global_order(trace)
    _agents_before_keys(trace)
    assert (counts["agents_reowned"], counts["agents_suspended"]) == (1, 1)
    assert (await world.agent_state(registered_id))[:2] == (owner, "active")
    assert (await world.agent_state(served_id))[:2] == (owner, "suspended")


@pytest.mark.asyncio
async def test_an_admin_deletion_that_settles_agents_takes_the_global_order(
    db_session, connector_world
):
    world = connector_world
    subject = await world.user("departing")
    owner, registered_id, served_id = await _agent_world(world, subject)
    await db_session.commit()

    with _LockTrace(db_session.bind) as trace:
        response = await delete_user(subject, {"user_id": "operator"}, db_session)

    _assert_global_order(trace)
    _agents_before_keys(trace)
    assert response["reowned"]["agents"] == 1
    assert response["suspended"] == {"agents": 1}


# ---------------------------------------------------------------------------
# Referral bonus recompute (#2443)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Referral:
    referrer: str
    referee: str
    code: str
    referrer_ws: UUID
    referee_ws: UUID | None


async def _referral(world, *, referee_owns_a_workspace: bool = True) -> _Referral:
    """A referrer with a code and one workspace, and a new referee."""
    referrer = await world.user("referrer")
    referee = await world.user("referee")
    referrer_ws = (await world.workspace(referrer)).id
    referee_ws = (await world.workspace(referee)).id if referee_owns_a_workspace else None
    code = f"code{uuid4().hex[:16]}"
    await world.db.execute(update(User).where(User.user_id == referrer).values(referral_code=code))
    await world.db.commit()
    return _Referral(referrer, referee, code, referrer_ws, referee_ws)


async def _bonus(db, workspace_id) -> int:
    return await db.scalar(
        select(Workspace.referral_memory_bonus).where(Workspace.id == workspace_id)
    )


@pytest.mark.asyncio
async def test_redeem_and_revoke_take_the_global_order(db_session, referral_world):
    """``redeem`` holds the referrer's users row from its cap check on, so it
    takes the workspace rows ``NOWAIT``: before its INSERT (whose foreign keys
    take a KEY SHARE on them) and again for the recompute. ``revoke`` takes
    them ``NOWAIT`` too."""
    referral = await _referral(referral_world)
    settings = get_settings()

    with _LockTrace(db_session.bind) as trace:
        grant = await ReferralService(db_session).redeem(
            referred_user_id=referral.referee, code=referral.code
        )
    _assert_global_order(trace)
    (redeem_tx,) = trace.kinds()
    assert redeem_tx[0] == ("users", False), redeem_tx
    assert len(redeem_tx) > 1 and set(redeem_tx[1:]) == {("ws_live", True)}, redeem_tx
    assert await _bonus(db_session, referral.referrer_ws) == (
        settings.referral_referrer_reward_memories
    )
    assert (
        await _bonus(db_session, referral.referee_ws) == settings.referral_referee_reward_memories
    )
    await db_session.commit()

    with _LockTrace(db_session.bind) as trace:
        _, changed = await ReferralService(db_session).revoke(grant_id=grant.id, reason="test")
        await db_session.commit()
    assert changed
    _assert_global_order(trace)
    (revoke_tx,) = trace.kinds()
    workspace_locks = [lock for lock in revoke_tx if lock[0] == "ws_live"]
    assert workspace_locks and set(workspace_locks) == {("ws_live", True)}, revoke_tx
    assert await _bonus(db_session, referral.referrer_ws) == 0
    assert await _bonus(db_session, referral.referee_ws) == 0


@pytest.mark.asyncio
async def test_applying_pending_grants_takes_the_workspace_nowait(db_session, referral_world):
    """A grant recorded while the referee owned no workspace is applied once
    one exists; its recompute takes the workspace row ``NOWAIT`` as well."""
    world = referral_world
    referral = await _referral(world, referee_owns_a_workspace=False)
    await ReferralService(db_session).redeem(referred_user_id=referral.referee, code=referral.code)
    referee_ws = (await world.workspace(referral.referee)).id
    await db_session.commit()

    with _LockTrace(db_session.bind) as trace:
        applied = await ReferralService(db_session).apply_pending_grants(referral.referee)
    assert applied == 1
    _assert_global_order(trace)
    (apply_tx,) = trace.kinds()
    assert ("ws_live", True) in apply_tx and ("ws_live", False) not in apply_tx, apply_tx
    assert await _bonus(db_session, referee_ws) == get_settings().referral_referee_reward_memories


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["referrer_ws", "referee_ws"])
async def test_a_redeem_does_not_wait_on_a_workspace_another_transaction_holds(
    db_session, referral_world, held
):
    """Another transaction holds a workspace row the redeem credits, as
    ``DELETE /workspaces/W`` does before it locks the users rows it clears.
    The redeem, which holds the referrer's users row, does not wait on it
    (neither in its INSERT's foreign-key check nor in the recompute): after
    its tries it raises ``ConflictError`` (409) and nothing is paid. Once the
    holder commits, the same redeem goes through."""
    referral = await _referral(referral_world)
    held_id = getattr(referral, held)
    settings = get_settings()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    pids: dict = {}

    async def redeem() -> UUID:
        async with maker() as db:
            pids["redeem"] = await _pid(db)
            grant = await ReferralService(db).redeem(
                referred_user_id=referral.referee, code=referral.code
            )
            return grant.id

    tasks: list[asyncio.Task] = []
    try:
        async with maker() as holder:
            await holder.execute(
                select(Workspace.id).where(Workspace.id == held_id).with_for_update()
            )
            redeeming = asyncio.create_task(redeem())
            tasks.append(redeeming)
            await _done_or_blocked(maker, redeeming, lambda: pids.get("redeem"))
            assert redeeming.done(), "the redeem waits on the workspace row"
            with pytest.raises(ConflictError):
                await redeeming
            await holder.commit()
    finally:
        await _cancel_all(tasks)

    grants = await db_session.scalar(
        select(func.count())
        .select_from(ReferralGrant)
        .where(ReferralGrant.referred_user_id == referral.referee)
    )
    assert grants == 0
    assert await _bonus(db_session, referral.referrer_ws) == 0
    assert await _bonus(db_session, referral.referee_ws) == 0

    grant_id = await asyncio.wait_for(redeem(), 15)
    paid = (
        await db_session.execute(
            select(ReferralGrant.referred_user_id, ReferralGrant.referrer_workspace_id).where(
                ReferralGrant.id == grant_id
            )
        )
    ).one()
    assert tuple(paid) == (referral.referee, referral.referrer_ws)
    assert await _bonus(db_session, referral.referrer_ws) == (
        settings.referral_referrer_reward_memories
    )
    assert (
        await _bonus(db_session, referral.referee_ws) == settings.referral_referee_reward_memories
    )


@pytest.mark.asyncio
async def test_a_redeem_that_loses_a_try_goes_on_in_its_transaction(db_session, referral_world):
    """The workspace row the redeem locks second is held, and the holder
    commits during the backoff after the lost try. That try rolls back only
    its savepoint: the redeem goes on in the same transaction, still holding
    the referrer's users row and the first workspace row, and pays both."""
    referral = await _referral(referral_world)
    assert referral.referee_ws is not None
    second = max(referral.referrer_ws, referral.referee_ws)  # the UUID order
    settings = get_settings()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async with maker() as holder:
        await holder.execute(select(Workspace.id).where(Workspace.id == second).with_for_update())

        async def release_then_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await holder.commit()
            await real_sleep(0)

        with (
            patch("services.workspace_locks.asyncio", SimpleNamespace(sleep=release_then_sleep)),
            _LockTrace(db_session.bind) as trace,
        ):
            async with maker() as db:
                await asyncio.wait_for(
                    ReferralService(db).redeem(
                        referred_user_id=referral.referee, code=referral.code
                    ),
                    15,
                )

    assert sleeps == [NOWAIT_LOCK_BACKOFF_SECONDS]
    _assert_global_order(trace)
    # one transaction: the lost try did not end it
    (redeem_tx,) = trace.kinds()
    assert redeem_tx[0] == ("users", False), redeem_tx
    assert set(redeem_tx[1:]) == {("ws_live", True)}, redeem_tx
    assert await _bonus(db_session, referral.referrer_ws) == (
        settings.referral_referrer_reward_memories
    )
    assert (
        await _bonus(db_session, referral.referee_ws) == settings.referral_referee_reward_memories
    )
