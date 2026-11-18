"""Shared workspace-row lock — the single synchronization point for every
owner-mutating path (#1102).

Workspace ownership is reassigned from three places: ``WorkspaceOwnershipService.
transfer_ownership`` (voluntary), ``WorkspaceService.update_member_role`` (every
role change since #2571: an admin <-> member change moves who the owner erasure
can hand the workspace to), and ``account_erasure_service`` (owner erasure
auto-transfer). All three acquire THIS ``SELECT ... FOR UPDATE`` on the
workspace row before mutating ownership, so concurrent owner-mutations on the
same workspace **serialize** (row-level mutual exclusion) instead of racing —
e.g. two paths can no longer read the same ``ownership_epoch`` and both write
``epoch + 1``, losing an increment. (The lock serializes the *paths*; it does not
itself reconcile the ``owner_user_id`` pointer with ``WorkspaceMember.role`` —
see ``workspace_ownership_service`` for that representation contract.)

``retry_nowait`` is for a path that must not wait on a workspace row: by the
global lock order (``tests/integration/test_erasure_lock_order_db.py``), any
transaction holding a ``users`` row. It takes the row ``NOWAIT`` and tries
again for a moment before it gives up with a 409 (#2443).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Workspace
from utils.db_helpers import is_lock_not_available
from utils.exceptions import ConflictError, NotFoundException

_T = TypeVar("_T")

# #2443: a NOWAIT lock that loses to a short holder (a memory create keeps its
# workspace row locked from its quota check until it commits) is tried again,
# each try under a savepoint: 0.05 + 0.1 + 0.2 + 0.4 s of sleep at most.
# Sleeping is not waiting on a lock, so the caller is in no lock cycle while it
# holds its users row. The same values as the erasure steps'
# ``account_erasure_service.WORKSPACE_LOCK_TRIES`` and
# ``account_erasure_service.WORKSPACE_LOCK_BACKOFF_SECONDS``.
NOWAIT_LOCK_TRIES = 5
NOWAIT_LOCK_BACKOFF_SECONDS = 0.05


async def retry_nowait(
    db: AsyncSession,
    lock: Callable[[], Awaitable[_T]],
    *,
    tries: int = NOWAIT_LOCK_TRIES,
    backoff: float = NOWAIT_LOCK_BACKOFF_SECONDS,
) -> _T:
    """Run ``lock``, a ``NOWAIT`` row lock, under a savepoint; while the row is
    busy (``55P03``) roll back to the savepoint, sleep and try again, ``tries``
    times in all, the sleep doubling from ``backoff`` (#2443).

    The savepoint keeps the surrounding transaction usable after a lost try:
    the failed statement aborts only the savepoint.

    Args:
        db: The session whose transaction takes the lock.
        lock: Issues the ``NOWAIT`` locking statement.
        tries: How many times to try.
        backoff: Seconds of sleep after the first lost try.

    Returns:
        What ``lock`` returned.

    Raises:
        ConflictError: The row is still busy after the last try (409). The
            caller's transaction is still open; the caller rolls it back.
    """
    for attempt in range(tries):
        try:
            async with db.begin_nested():
                return await lock()
        except DBAPIError as exc:
            if not is_lock_not_available(exc):
                raise
            if attempt + 1 == tries:
                raise ConflictError("Workspace is being modified; retry the operation") from exc
        await asyncio.sleep(backoff * 2**attempt)
    raise AssertionError("unreachable")  # pragma: no cover


async def lock_workspace_for_update(
    db: AsyncSession, workspace_id: UUID, *, nowait: bool = False
) -> Workspace:
    """``SELECT ... FOR UPDATE`` the live (non-soft-deleted) workspace row.

    The lock is the single-owner synchronization point: every path that reassigns
    OWNER must hold it, so two owner-mutations on the same workspace serialize
    instead of racing. The row lock is held until the surrounding transaction
    commits or rolls back (it cannot be released early — callers must keep the
    mutation in the same transaction).

    Args:
        db: The request-scoped async session.
        workspace_id: The workspace to lock.

    Returns:
        The locked :class:`Workspace` row.

    Raises:
        NotFoundException: the workspace is missing or soft-deleted (404).
    """
    try:
        workspace = (
            await db.execute(
                select(Workspace)
                .where(Workspace.id == workspace_id, Workspace.deleted_at.is_(None))
                .with_for_update(nowait=nowait)
                # populate_existing is load-bearing: if the row is ALREADY in this
                # session's identity map (e.g. account_erasure loaded it with an
                # unlocked SELECT), a plain re-SELECT returns the cached instance with
                # its STALE attributes — the FOR UPDATE takes the DB lock but discards
                # the freshly-read values. populate_existing forces the under-lock row
                # to overwrite the cached attributes, so callers see committed concurrent
                # changes (owner_user_id / ownership_epoch) and read-modify-write is safe.
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if is_lock_not_available(exc):
            raise ConflictError("Workspace is being modified; retry the operation") from exc
        raise
    if workspace is None:
        raise NotFoundException("Workspace")
    return workspace


async def lock_workspace_nowait(
    db: AsyncSession, workspace_id: UUID, *, shared: bool = False
) -> Workspace:
    """Lock the live workspace row ``NOWAIT`` through :func:`retry_nowait`.

    For a path that holds a ``users`` row and so must not wait on the workspace
    row (#2443). ``shared=False`` takes ``FOR UPDATE``. ``shared=True`` takes
    ``FOR SHARE`` (#2914): it still conflicts with the ``FOR UPDATE`` that
    ownership transfers, role changes, erasure and workspace deletion take and
    with every ``UPDATE`` of the row (``FOR NO KEY UPDATE``, e.g. an
    ``ownership_epoch`` bump), but not with other ``FOR SHARE`` readers or with
    the ``FOR KEY SHARE`` a foreign-key insert takes.

    Args:
        db: The request-scoped async session.
        workspace_id: The workspace to lock.
        shared: Take ``FOR SHARE`` instead of ``FOR UPDATE``.

    Returns:
        The locked :class:`Workspace` row, refreshed from the database.

    Raises:
        NotFoundException: the workspace is missing or soft-deleted (404).
        ConflictError: the row is still locked after the tries (409).
    """

    async def lock() -> Workspace | None:
        return await db.scalar(
            select(Workspace)
            .where(Workspace.id == workspace_id, Workspace.deleted_at.is_(None))
            .with_for_update(read=shared, nowait=True)
            # Overwrite a cached instance with the row read under the lock
            # (see lock_workspace_for_update).
            .execution_options(populate_existing=True)
        )

    workspace = await retry_nowait(db, lock)
    if workspace is None:
        raise NotFoundException("Workspace")
    return workspace
