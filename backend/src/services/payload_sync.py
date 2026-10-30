"""The post-commit vector payload step, shared by its callers (#2283).

A write that changes a memory's payload fields commits in Postgres first and
patches the vector payload after: no vector-store round trip happens while
the rows are locked by the write. Session rollback (``session_rollback.
finish``) and the erasure's details restore (``erasure_details.
sync_restored_payloads``) both run that step the same way, and this module is
that way:

- the rows are worked in groups of ``PAYLOAD_SYNC_GROUP``, one transaction
  each, in the order the caller gives them;
- a group's rows are read ``FOR SHARE`` in id order and held until the group
  commits, so an edit that lands meanwhile waits and then syncs its own
  payload over this one, and no row waits on more than one group's round
  trips;
- the caller plans each row from what was read (``plan``: skip it, or a
  ``PlannedPatch``) and applies it (``apply``: the vector-store call);
- what happens when a read, a patch or the group's commit fails is the
  caller's ``PayloadFailurePolicy``: ``SwallowFailures`` (log and go on; the
  payload keeps its old values until the row's next edit or re-embed; a
  group that cannot be read is skipped, never patched from values queued
  before an edit that may have committed since) or
  ``RequeueEmbeddingOnFailure`` (the row goes back to the embedding queue,
  whose upsert rewrites the whole payload from the row).

Nothing here imports the callers, so both import it at module load.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from utils.exceptions import QdrantError
from utils.logger import get_logger

logger = get_logger(__name__)

#: Rows one payload-sync transaction holds ``FOR SHARE`` while it patches them.
PAYLOAD_SYNC_GROUP = 50

ItemT = TypeVar("ItemT")
DataT = TypeVar("DataT")


@dataclass(frozen=True)
class PlannedPatch(Generic[DataT]):
    """One row's payload patch, as the caller's ``plan`` built it.

    Attributes:
        memory_id: The memory.
        data: What the caller's ``apply`` needs (the row, the patch, ...).
        embedded: Whether the row's embedding had succeeded when planned —
            read before the patch, since a failed SAVEPOINT expires the row.
        log_fields: Extra fields for the policy's failure log lines (ids
            only, never values); a key the policy sets itself is dropped.
    """

    memory_id: UUID
    data: DataT
    embedded: bool = False
    log_fields: Mapping[str, Any] = field(default_factory=dict)


class PayloadFailurePolicy(Protocol):
    """What the payload step does when a read, a patch or a commit fails.

    No method may fail the caller, for the vector store's sake or for a
    session that cannot roll back: the Postgres work is committed already.
    """

    async def read_failed(self, db: AsyncSession, ids: list[UUID], exc: Exception) -> bool:
        """The group's ``FOR SHARE`` read failed.

        Returns:
            True to plan the group without rows (from what the caller queued),
            False to skip the group.
        """
        ...

    async def patch_failed(
        self, db: AsyncSession | None, patch: PlannedPatch[Any], exc: Exception
    ) -> None:
        """``apply`` raised for ``patch``."""
        ...

    async def commit_failed(self, db: AsyncSession, ids: list[UUID], exc: Exception) -> None:
        """The group's commit (which releases its share locks) failed."""
        ...


class SwallowFailures:
    """Log and go on: the payload keeps its old values until the row's next
    edit or re-embed (session rollback's policy).

    A group whose read fails is rolled back and skipped (#2346): what the
    caller queued may be older than an edit committed since, so it is not
    written. A group whose commit fails is rolled back, so the next group's
    read runs in a fresh transaction. A rollback that fails too is logged
    (``<prefix>_tx_end_failed``), never raised. Events are
    ``<prefix>_reread_failed``, ``<prefix>_sync_failed`` and
    ``<prefix>_tx_end_failed``; logs carry ids only, never values.

    Attributes:
        failed: Patches that failed, and rows of groups that could not be
            read (not patched).
    """

    def __init__(self, event_prefix: str) -> None:
        self.event_prefix = event_prefix
        self.failed = 0

    async def read_failed(self, db: AsyncSession, ids: list[UUID], exc: Exception) -> bool:
        await _rollback_or_log(db, self.event_prefix)
        self.failed += len(ids)
        logger.warning(
            f"{self.event_prefix}_reread_failed",
            memory_ids=[str(i) for i in ids],
            error_class=type(exc).__name__,
        )
        return False

    async def patch_failed(
        self, db: AsyncSession | None, patch: PlannedPatch[Any], exc: Exception
    ) -> None:
        self.failed += 1
        logger.warning(
            f"{self.event_prefix}_sync_failed",
            memory_id=str(patch.memory_id),
            **_extra(patch, "error_class"),
            error_class=type(exc).__name__,
        )

    async def commit_failed(self, db: AsyncSession, ids: list[UUID], exc: Exception) -> None:
        _log_tx_end_failed(self.event_prefix, exc)
        await _rollback_or_log(db, self.event_prefix)


class RequeueEmbeddingOnFailure:
    """Degrade a failed patch to an embedding re-queue (the erasure's policy).

    A row whose patch fails — for any reason, the vector store or a read in
    between — is re-queued for embedding (``requeue_embedding``: the sweep's
    upsert rewrites the whole payload from the row); a row not ``success`` is
    queued already. A re-queue that fails too is counted and logged, and the
    next row goes on. A group whose read fails is skipped and its rows
    counted as ``requeue_failed``. A group whose commit fails is rolled back:
    its patches reached the vector store, but the re-queues made inside its
    transaction are lost — the ``<prefix>_commit_failed`` line's ids are the
    operator's list. A rollback that fails too is logged
    (``<prefix>_tx_end_failed``), never raised. Events are
    ``<prefix>_failed``, ``<prefix>_requeue_failed``, ``<prefix>_read_failed``,
    ``<prefix>_commit_failed`` and ``<prefix>_tx_end_failed``; logs carry ids
    only, never values.

    Attributes:
        requeued_after_error: Failed patches on embedded rows, re-queued.
        skipped_requeued: Failed patches on rows queued already.
        requeue_failed: Rows whose re-queue failed too, or whose group could
            not be read.
    """

    def __init__(self, event_prefix: str, *, reason: str) -> None:
        self.event_prefix = event_prefix
        self.reason = reason
        self.requeued_after_error = 0
        self.skipped_requeued = 0
        self.requeue_failed = 0

    async def read_failed(self, db: AsyncSession, ids: list[UUID], exc: Exception) -> bool:
        await _rollback_or_log(db, self.event_prefix)
        self.requeue_failed += len(ids)
        logger.warning(
            f"{self.event_prefix}_read_failed",
            memory_ids=[str(i) for i in ids],
            error_class=type(exc).__name__,
        )
        return False

    async def patch_failed(
        self, db: AsyncSession | None, patch: PlannedPatch[Any], exc: Exception
    ) -> None:
        from services.content_chunk_writer import requeue_embedding

        try:
            if patch.embedded and db is None:
                raise RuntimeError("no session to re-queue the embedding")
            requeued = patch.embedded and await requeue_embedding(
                cast(AsyncSession, db), patch.memory_id, reason=self.reason
            )
        except Exception as requeue_exc:
            self.requeue_failed += 1
            logger.warning(
                f"{self.event_prefix}_requeue_failed",
                memory_id=str(patch.memory_id),
                **_extra(patch, "error_class", "requeue_error_class"),
                error_class=type(exc).__name__,
                requeue_error_class=type(requeue_exc).__name__,
            )
            return
        if requeued:
            self.requeued_after_error += 1
        else:
            self.skipped_requeued += 1
        # a row queued already may have no point yet: not a fault
        (logger.warning if requeued or not isinstance(exc, QdrantError) else logger.info)(
            f"{self.event_prefix}_failed",
            memory_id=str(patch.memory_id),
            **_extra(patch, "error_class", "requeued"),
            error_class=type(exc).__name__,
            requeued=requeued,
        )

    async def commit_failed(self, db: AsyncSession, ids: list[UUID], exc: Exception) -> None:
        await _rollback_or_log(db, self.event_prefix)
        logger.warning(
            f"{self.event_prefix}_commit_failed",
            memory_ids=[str(i) for i in ids],
            error_class=type(exc).__name__,
        )


def _log_tx_end_failed(event_prefix: str, exc: Exception) -> None:
    logger.warning(f"{event_prefix}_tx_end_failed", error_class=type(exc).__name__)


async def _rollback_or_log(db: AsyncSession, event_prefix: str) -> None:
    """Roll the group's transaction back; a failed rollback is logged, not raised."""
    try:
        await db.rollback()
    except Exception as exc:
        _log_tx_end_failed(event_prefix, exc)


def _extra(patch: PlannedPatch[Any], *own: str) -> dict[str, Any]:
    """``patch.log_fields`` without the keys a failure log line sets itself."""
    return {k: v for k, v in patch.log_fields.items() if k not in {"memory_id", *own}}


async def share_lock_rows(
    db: AsyncSession,
    ids: Sequence[UUID],
    *,
    columns: Sequence[Any] | None = None,
    live_only: bool = False,
) -> dict[UUID, Any]:
    """``ids``' rows, locked ``FOR SHARE`` in id order.

    Args:
        db: The session (its transaction holds the locks).
        ids: The memories.
        columns: The columns to read; None reads the ``Memory`` rows, with
            ``populate_existing`` so a row the session already holds is
            refreshed from the locked read.
        live_only: Leave out tombstones.
    """
    stmt = select(Memory) if columns is None else select(*columns)
    stmt = stmt.where(Memory.id.in_(ids))
    if live_only:
        stmt = stmt.where(Memory.deleted_at.is_(None))
    stmt = stmt.order_by(Memory.id).with_for_update(read=True)
    if columns is None:
        stmt = stmt.execution_options(populate_existing=True)
        return {memory.id: memory for memory in (await db.execute(stmt)).scalars()}
    return {row.id: row for row in await db.execute(stmt)}


async def sync_payloads_after_commit(
    db: AsyncSession | None,
    items: Sequence[ItemT],
    *,
    memory_id: Callable[[ItemT], UUID],
    plan: Callable[[ItemT, Mapping[UUID, Any] | None], PlannedPatch[DataT] | None],
    apply: Callable[[PlannedPatch[DataT]], Awaitable[None]],
    on_failure: PayloadFailurePolicy,
    columns: Sequence[Any] | None = None,
    live_only: bool = False,
) -> None:
    """Patch ``items``' vector payloads after the caller's commit, in groups.

    Called AFTER the caller's transaction committed, in the same session (each
    group is a fresh transaction). Per group of ``PAYLOAD_SYNC_GROUP`` items,
    in the order given: the rows are read ``FOR SHARE`` in id order
    (``share_lock_rows``), each item is planned (``plan(item, rows)``; None
    skips it) and applied (``apply``), and the group commits, releasing the
    locks. A failure goes to ``on_failure``; nothing here raises for the
    vector store's sake.

    Args:
        db: The session, its write committed; None applies what ``plan``
            builds without reading (and without a transaction).
        items: What to patch, in group order.
        memory_id: An item's memory id.
        plan: Builds an item's patch from the group's locked rows (None when
            not read: no session, or the policy chose to go on after a failed
            read); returns None to skip the item. Counters for skips are the
            caller's.
        apply: Applies a planned patch; an exception goes to
            ``on_failure.patch_failed``.
        on_failure: The failure policy.
        columns: ``share_lock_rows``' columns (None: the ``Memory`` rows).
        live_only: ``share_lock_rows``' tombstone filter.
    """
    for start in range(0, len(items), PAYLOAD_SYNC_GROUP):
        group = items[start : start + PAYLOAD_SYNC_GROUP]
        ids = [memory_id(item) for item in group]
        rows: dict[UUID, Any] | None = None
        if db is not None:
            try:
                rows = await share_lock_rows(db, ids, columns=columns, live_only=live_only)
            except Exception as exc:
                if not await on_failure.read_failed(db, ids, exc):
                    continue
        for item in group:
            patch = plan(item, rows)
            if patch is None:
                continue
            try:
                await apply(patch)
            except Exception as exc:
                await on_failure.patch_failed(db, patch, exc)
        if rows is not None and db is not None:
            try:
                await db.commit()  # releases the share locks
            except Exception as exc:
                await on_failure.commit_failed(db, ids, exc)


__all__ = [
    "PAYLOAD_SYNC_GROUP",
    "PayloadFailurePolicy",
    "PlannedPatch",
    "RequeueEmbeddingOnFailure",
    "SwallowFailures",
    "share_lock_rows",
    "sync_payloads_after_commit",
]
