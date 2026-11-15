"""A deletion of an admin and a role change leave one system admin (#2855).

``DELETE /api/v1/admin/users/{user_id}`` and an admin erasure
(``POST /api/v1/admin/users/{user_id}/erase``, ``AccountErasureService``)
check the initial/last admin rule with ``SystemAdminService.can_delete_admin``,
under the lock of ``lock_for_role_change``: one statement locks every admin
row and the target (and, for the delete, every ``users`` row the deletion
locks later), in ``user_id`` order, ``FOR NO KEY UPDATE``, and the admin
count is read after it. The lock is held to the deletion's commit, so a
deletion and a demotion, or two deletions, run one after the other and the
second one counts what the first committed. An erasure checks once when it is
requested (early answer, no request row) and again when it runs, in the
transaction that deletes the account.

Each test drives real Postgres sessions and waits on ``pg_blocking_pids``
before it releases the paused one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.user_deletion_service as deletion_module
from api.routes.admin import UserDeleteRequest, delete_user
from models.auth import AuditLog, User, Workspace
from models.erasure import (
    REASON_USER_REQUEST_VIA_SUPPORT,
    STATUS_COMPLETE,
    STATUS_FAILED,
    ErasureRequest,
)
from services.account_erasure_service import AccountErasureService
from utils.exceptions import AdminProtectionError, ErasureForbiddenError

from .test_erasure_lock_order_db import (
    _assert_global_order,
    _external_stores_stubbed,
    _LockTrace,
)
from .test_system_admin_demotion_concurrency_db import (
    LAST_ADMIN_MESSAGE,
    OPERATOR,
    _demote,
    wait_for_block,
)

pytestmark = pytest.mark.timeout(90)

DELETE_LAST_ADMIN = "Cannot delete the last remaining system administrator"
DEMOTE_ROUTES = ["PUT", "DELETE"]

Operation = Callable[[AsyncSession], Awaitable[Any]]


@dataclass
class Admins:
    """Accounts of one test; the admins among them are the only admins."""

    db: AsyncSession
    world: Any

    @property
    def maker(self) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(self.db.bind, expire_on_commit=False)

    async def account(self, prefix: str, *, admin: bool) -> str:
        """A ``users`` row; ``prefix`` orders it by ``user_id``."""
        user_id = await self.world.user(prefix)
        if admin:
            await self.db.execute(update(User).where(User.user_id == user_id).values(role="admin"))
        await self.db.commit()
        return user_id

    async def admin_ids(self) -> list[str]:
        await self.db.rollback()
        return list(
            (
                await self.db.execute(
                    select(User.user_id).where(User.role == "admin").order_by(User.user_id)
                )
            )
            .scalars()
            .all()
        )

    async def exists(self, user_id: str) -> bool:
        await self.db.rollback()
        return (
            await self.db.scalar(
                select(func.count()).select_from(User).where(User.user_id == user_id)
            )
        ) == 1

    async def role_audit_targets(self) -> list[str]:
        """The account each role audit row of the operator is about."""
        await self.db.rollback()
        rows = (
            await self.db.execute(
                select(AuditLog.action, AuditLog.user_id, AuditLog.user_metadata).where(
                    AuditLog.action.in_(("system_role_update", "system_admin_demote")),
                    AuditLog.user_email == OPERATOR["email"],
                )
            )
        ).all()
        return sorted(
            row.user_metadata["target_user_id"]
            if row.action == "system_role_update"
            else row.user_id
            for row in rows
        )

    async def erasure_requests(self, user_id: str) -> list[ErasureRequest]:
        await self.db.rollback()
        return list(
            (
                await self.db.execute(
                    select(ErasureRequest)
                    .where(ErasureRequest.user_id == user_id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )


@pytest_asyncio.fixture
async def accounts(db_session: AsyncSession, connector_world) -> AsyncIterator[Admins]:
    """Admin rows other tests left behind are set aside (role ``user``) and
    restored afterwards; the audit rows written during the test are removed
    (``connector_world`` removes the accounts, workspaces and erasure rows)."""
    await db_session.rollback()
    others = list(
        (await db_session.execute(select(User.user_id).where(User.role == "admin"))).scalars().all()
    )
    if others:
        await db_session.execute(update(User).where(User.user_id.in_(others)).values(role="user"))
    last_audit = await db_session.scalar(select(func.coalesce(func.max(AuditLog.id), 0)))
    await db_session.commit()

    with _external_stores_stubbed():
        yield Admins(db_session, connector_world)

    await db_session.rollback()
    await db_session.execute(delete(AuditLog).where(AuditLog.id > last_audit))
    if others:
        await db_session.execute(update(User).where(User.user_id.in_(others)).values(role="admin"))
    await db_session.commit()


def delete_of(user_id: str, request: UserDeleteRequest | None = None) -> Operation:
    async def run(db: AsyncSession):
        return await delete_user(user_id, OPERATOR, db, request)

    return run


def demotion_of(route: str, user_id: str) -> Operation:
    async def run(db: AsyncSession):
        return await _demote(route, db, user_id)

    return run


def erasure_of(user_id: str) -> Operation:
    async def run(db: AsyncSession):
        return await AccountErasureService(db).admin_force_erase(
            target_user_id=user_id,
            initiator_user_id=OPERATOR["user_id"],
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            reason_detail="integration test",
        )

    return run


async def race(
    maker: async_sessionmaker[AsyncSession],
    first: Operation,
    second: Operation,
    *,
    first_commit: int = 1,
) -> tuple[Any, Any]:
    """``first`` pauses at its ``first_commit``-th commit with its locks held;
    ``second`` starts then and is seen waiting on them; ``first`` commits.

    Returns both outcomes, a result or the exception raised.
    """
    gap, resume, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    second_pid = None

    async def one():
        async with maker() as db:
            commit = db.commit
            calls = 0

            async def paused_commit():
                nonlocal calls
                calls += 1
                if calls == first_commit:
                    gap.set()
                    await asyncio.wait_for(resume.wait(), 15)
                await commit()

            db.commit = paused_commit  # type: ignore[method-assign]
            return await first(db)

    async def two():
        nonlocal second_pid
        async with maker() as db:
            second_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            return await second(db)

    tasks: list[asyncio.Task] = []
    try:
        task_one = asyncio.create_task(one())
        tasks.append(task_one)
        await asyncio.wait_for(gap.wait(), 15)
        task_two = asyncio.create_task(two())
        tasks.append(task_two)
        await asyncio.wait_for(started.wait(), 15)
        # The second request is seen waiting on the first one's row locks.
        await asyncio.wait_for(wait_for_block(maker, second_pid), 5)
        assert not task_two.done()
        resume.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(task_one, task_two, return_exceptions=True), 30
        )
    finally:
        resume.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return outcomes[0], outcomes[1]


def _ok(outcome: Any) -> Any:
    assert not isinstance(outcome, BaseException), repr(outcome)
    return outcome


def _assert_delete_refused(outcome: Any) -> None:
    assert isinstance(outcome, AdminProtectionError), repr(outcome)
    assert outcome.status_code == 403
    assert outcome.error_code == "ADMIN-001"
    assert outcome.message == DELETE_LAST_ADMIN


def _assert_demotion_refused(outcome: Any) -> None:
    assert isinstance(outcome, AdminProtectionError), repr(outcome)
    assert outcome.status_code == 403
    assert outcome.error_code == "ADMIN-001"
    assert outcome.reason == "last_admin"
    assert outcome.message == LAST_ADMIN_MESSAGE


async def _two_admins(accounts: Admins, deleted_holds: str) -> tuple[str, str]:
    """Two admins, the first one returned is the one deleted."""
    lower = await accounts.account("a-admin", admin=True)
    higher = await accounts.account("b-admin", admin=True)
    return (lower, higher) if deleted_holds == "lower_user_id" else (higher, lower)


# --- a deletion and a demotion -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", DEMOTE_ROUTES)
@pytest.mark.parametrize("deleted_holds", ["lower_user_id", "higher_user_id"])
async def test_a_delete_then_a_demotion_of_the_other_admin_leave_one_admin(
    accounts, route, deleted_holds
):
    deleted, demoted = await _two_admins(accounts, deleted_holds)

    deletion, demotion = await race(accounts.maker, delete_of(deleted), demotion_of(route, demoted))

    assert _ok(deletion)["user_id"] == deleted
    _assert_demotion_refused(demotion)
    assert await accounts.admin_ids() == [demoted]
    assert not await accounts.exists(deleted)
    assert await accounts.role_audit_targets() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", DEMOTE_ROUTES)
@pytest.mark.parametrize("deleted_holds", ["lower_user_id", "higher_user_id"])
async def test_a_demotion_then_a_delete_of_the_other_admin_leave_one_admin(
    accounts, route, deleted_holds
):
    deleted, demoted = await _two_admins(accounts, deleted_holds)

    demotion, deletion = await race(accounts.maker, demotion_of(route, demoted), delete_of(deleted))

    assert _ok(demotion)
    _assert_delete_refused(deletion)
    assert await accounts.admin_ids() == [deleted]
    assert await accounts.exists(deleted)
    assert await accounts.role_audit_targets() == [demoted]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_holds", ["lower_user_id", "higher_user_id"])
async def test_two_deletes_of_the_last_two_admins_leave_one_admin(accounts, first_holds):
    first, second = await _two_admins(accounts, first_holds)

    one, two = await race(accounts.maker, delete_of(first), delete_of(second))

    assert _ok(one)["user_id"] == first
    _assert_delete_refused(two)
    assert await accounts.admin_ids() == [second]
    assert await accounts.exists(second)


# --- an erasure and a demotion ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", DEMOTE_ROUTES)
async def test_a_demotion_waits_on_the_running_erasure_of_the_other_admin(accounts, route):
    erased, demoted = await _two_admins(accounts, "lower_user_id")

    # The erasure's first commit is its request row; the second is the
    # Postgres step's, with the account deleted and the locks held.
    erasure, demotion = await race(
        accounts.maker, erasure_of(erased), demotion_of(route, demoted), first_commit=2
    )

    assert _ok(erasure).status == STATUS_COMPLETE
    _assert_demotion_refused(demotion)
    assert await accounts.admin_ids() == [demoted]
    assert not await accounts.exists(erased)
    assert await accounts.role_audit_targets() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", DEMOTE_ROUTES)
async def test_an_erasure_requested_during_a_demotion_waits_and_is_refused(accounts, route):
    erased, demoted = await _two_admins(accounts, "lower_user_id")

    demotion, erasure = await race(accounts.maker, demotion_of(route, demoted), erasure_of(erased))

    assert _ok(demotion)
    assert isinstance(erasure, ErasureForbiddenError), repr(erasure)
    assert erasure.status_code == 403
    assert erasure.error_code == "ERASURE-003"
    assert erasure.message == DELETE_LAST_ADMIN
    assert await accounts.erasure_requests(erased) == []
    assert await accounts.admin_ids() == [erased]
    assert await accounts.role_audit_targets() == [demoted]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", DEMOTE_ROUTES)
async def test_a_demotion_committed_before_the_erasure_runs_fails_the_request(accounts, route):
    erased, demoted = await _two_admins(accounts, "lower_user_id")
    maker = accounts.maker
    requested, resume = asyncio.Event(), asyncio.Event()
    original = AccountErasureService._execute

    async def paused_execute(self, request, target):
        # The request row is committed; its run has not started.
        requested.set()
        await asyncio.wait_for(resume.wait(), 15)
        return await original(self, request, target)

    async def erase():
        async with maker() as db:
            return await erasure_of(erased)(db)

    with patch.object(AccountErasureService, "_execute", paused_execute):
        task = asyncio.create_task(erase())
        try:
            await asyncio.wait_for(requested.wait(), 15)
            async with maker() as db:
                await asyncio.wait_for(_demote(route, db, demoted), 5)
            resume.set()
            with pytest.raises(ErasureForbiddenError) as refused:
                await asyncio.wait_for(task, 30)
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert refused.value.status_code == 403
    assert refused.value.error_code == "ERASURE-003"
    assert refused.value.message == DELETE_LAST_ADMIN
    [request] = await accounts.erasure_requests(erased)
    assert request.status == STATUS_FAILED
    assert request.failure_reason == DELETE_LAST_ADMIN
    assert await accounts.exists(erased)
    assert await accounts.admin_ids() == [erased]


# --- what the count reads, and what the lock does not wait on -----------------


@pytest.mark.asyncio
async def test_a_promotion_committed_while_a_delete_of_the_only_admin_waited_allows_it(accounts):
    only_admin = await accounts.account("b-admin", admin=True)
    promoted = await accounts.account("c-user", admin=False)

    # The role PUT locks the admin rows (the only admin) and its target.
    promotion, deletion = await race(
        accounts.maker,
        lambda db: _promote(db, promoted),
        delete_of(only_admin),
    )

    assert _ok(promotion)["new_role"] == "admin"
    assert _ok(deletion)["user_id"] == only_admin
    assert await accounts.admin_ids() == [promoted]


async def _promote(db: AsyncSession, user_id: str):
    from api.routes.admin import RoleUpdateRequest, update_user_role

    return await update_user_role(user_id, RoleUpdateRequest(role="admin"), OPERATOR, db)


@pytest.mark.asyncio
async def test_a_delete_does_not_wait_on_a_key_share_lock_of_another_admin_row(accounts):
    deleted = await accounts.account("a-admin", admin=True)
    held = await accounts.account("b-admin", admin=True)
    maker = accounts.maker
    async with maker() as holder, maker() as deleting:
        # An open insert that references a user holds ``FOR KEY SHARE`` on it.
        await holder.execute(
            select(User.user_id)
            .where(User.user_id == held)
            .with_for_update(read=True, key_share=True)
        )
        answer = await asyncio.wait_for(delete_of(deleted)(deleting), 5)
        await holder.rollback()

    assert answer["user_id"] == deleted
    assert await accounts.admin_ids() == [held]


@pytest.mark.asyncio
async def test_a_role_change_of_the_successor_during_an_admin_delete_does_not_deadlock(
    db_session, accounts
):
    # The successor sorts before the admin being deleted: a role change locks
    # it first, then waits on the admin rows.
    successor = await accounts.account("a-successor", admin=False)
    deleted = await accounts.account("b-admin", admin=True)
    await accounts.account("c-admin", admin=True)
    workspace_id = (await accounts.world.workspace(deleted, **{successor: "admin"})).id
    await db_session.commit()
    maker = accounts.maker
    checked, resume, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    role_change_pid = None
    original = deletion_module.transfer_user_ownership

    async def paused_transfer(*args, **kwargs):
        # After the admin check's lock statement, before the transfer's locks.
        checked.set()
        await asyncio.wait_for(resume.wait(), 15)
        return await original(*args, **kwargs)

    async def remove():
        async with maker() as db:
            return await delete_of(
                deleted, UserDeleteRequest(workspace_transfers={workspace_id: successor})
            )(db)

    async def promote():
        nonlocal role_change_pid
        async with maker() as db:
            role_change_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            return await _promote(db, successor)

    tasks: list[asyncio.Task] = []
    with patch.object(deletion_module, "transfer_user_ownership", paused_transfer):
        try:
            deletion = asyncio.create_task(remove())
            tasks.append(deletion)
            await asyncio.wait_for(checked.wait(), 15)
            promotion = asyncio.create_task(promote())
            tasks.append(promotion)
            await asyncio.wait_for(started.wait(), 15)
            await asyncio.wait_for(wait_for_block(maker, role_change_pid), 5)
            resume.set()
            deleted_answer, promoted_answer = await asyncio.wait_for(
                asyncio.gather(deletion, promotion, return_exceptions=True), 30
            )
        finally:
            resume.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    assert _ok(deleted_answer)["transferred_workspaces"] == {str(workspace_id): successor}
    assert _ok(promoted_answer)["new_role"] == "admin"
    assert not await accounts.exists(deleted)
    assert successor in await accounts.admin_ids()
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == workspace_id))
        == successor
    )


# --- the lock order ----------------------------------------------------------


def _first_lock_is_the_admin_rows_lock(transaction) -> None:
    first = transaction[0]
    assert first.kind == "users", first.sql[:200]
    assert "users.role = " in first.sql, first.sql[:200]
    assert first.sql.endswith("ORDER BY users.user_id FOR NO KEY UPDATE"), first.sql[-200:]


@pytest.mark.asyncio
async def test_an_admin_delete_takes_the_admin_rows_and_its_users_rows_first(db_session, accounts):
    successor = await accounts.account("a-successor", admin=False)
    deleted = await accounts.account("b-admin", admin=True)
    await accounts.account("c-admin", admin=True)
    workspace_id = (await accounts.world.workspace(deleted, **{successor: "admin"})).id
    await db_session.commit()
    request = UserDeleteRequest(workspace_transfers={workspace_id: successor})

    with _LockTrace(db_session.bind) as trace:
        async with accounts.maker() as db:
            await delete_of(deleted, request)(db)

    _assert_global_order(trace)
    [deletion] = [
        tx
        for tx in trace.transactions
        if ("users_dml", False) in [(lock.kind, lock.nowait) for lock in tx]
    ]
    _first_lock_is_the_admin_rows_lock(deletion)
    # The successor is in that first statement: the transfer's lock on it
    # only strengthens a row the deletion holds.
    assert "users.user_id IN" in deletion[0].sql


@pytest.mark.asyncio
async def test_an_admin_erasure_takes_the_admin_rows_first_in_both_transactions(
    db_session, accounts
):
    erased = await accounts.account("a-admin", admin=True)
    await accounts.account("b-admin", admin=True)

    with _LockTrace(db_session.bind) as trace:
        async with accounts.maker() as db:
            assert (await erasure_of(erased)(db)).status == STATUS_COMPLETE

    _assert_global_order(trace)
    with_users = [tx for tx in trace.transactions if any(lock.kind == "users" for lock in tx)]
    # The request (its check, then the request row) and the run (its check,
    # then the guard's lock of the account, the link lock, the deletes).
    assert len(with_users) >= 2
    for transaction in with_users:
        _first_lock_is_the_admin_rows_lock(transaction)
    [run] = [
        tx for tx in with_users if ("link", False) in [(lock.kind, lock.nowait) for lock in tx]
    ]
    # Then the guard's lock of the account, a row the run already holds.
    assert run[1].kind == "users"
    assert "WHERE users.id = " in run[1].sql
    assert run[1].sql.endswith("FOR UPDATE")


@pytest.mark.asyncio
async def test_with_no_admin_row_an_operator_deletes_and_erases_a_user(db_session, accounts):
    deleted = await accounts.account("a-user", admin=False)
    erased = await accounts.account("b-user", admin=False)
    assert await accounts.admin_ids() == []

    with _LockTrace(db_session.bind) as trace:
        async with accounts.maker() as db:
            assert (await delete_of(deleted)(db))["user_id"] == deleted
        async with accounts.maker() as db:
            assert (await erasure_of(erased)(db)).status == STATUS_COMPLETE

    _assert_global_order(trace)
    assert not await accounts.exists(deleted)
    assert not await accounts.exists(erased)
    assert await accounts.admin_ids() == []
