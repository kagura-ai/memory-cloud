"""Restore a soft-deleted context from its rows (#1804).

Deleting a context soft-deletes it and the memories it holds (#84), and since
v0.88.0 removes its points from the vector store (#1798). The rows stay in
Postgres until the tombstone purge (``CLEANUP_DELETED_MEMORIES_RETENTION_DAYS``,
default 30 days), so a deleted context can be brought back from them:

* the context row gets its ``deleted_at`` / ``deleted_by`` cleared;
* the memories the deletion tombstoned get theirs cleared too, and go back to
  ``embedding_status='pending'`` so the embedding sweep
  (``tasks/embedding_tasks.py``) builds their points again.

"The memories the deletion tombstoned" are the context's rows with the same
``deleted_by`` as the context, soft-deleted at the context's ``deleted_at``.
``delete_context`` stamps both with one timestamp from v0.90.0 on; before that
each memory got its own, a little earlier than the context's, so when no
memory carries the context's timestamp the restore takes those up to
``DELETION_WINDOW`` before it. A memory forgotten earlier, or tombstoned by
Sleep, stays deleted.

Not restored: what the deletion hard-deleted or rewrote — the context's
neural edges, its entries in members' ``allowed_context_ids``, revoked
resource tokens — and memories the purge already removed.

Used by ``POST /api/v1/admin/contexts/{context_id}/restore`` and
``python -m src.cli.restore_context``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import AuditLog, Context, Workspace
from models.memory import Memory
from services.context_service import CONTEXT_NAME_PATTERN, DEFAULT_CONTEXT_NAME
from utils.datetime import to_utc_iso
from utils.exceptions import ConflictError, NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# How long before the context's ``deleted_at`` a memory's ``deleted_at`` may
# fall and still count as the deletion's. Before v0.90.0 each memory was
# stamped as the deletion reached it, ahead of the context; the deletion is one
# transaction, so the spread is its duration.
DELETION_WINDOW = timedelta(minutes=10)

AUDIT_ACTION = "context_restore"


@dataclass
class ContextRestoreResult:
    """What a restore did, or would do on a dry run."""

    context_id: str
    workspace_id: str
    name: str
    deleted_at: datetime
    deleted_by: str | None
    dry_run: bool
    # Memories the deletion tombstoned that are (or would be) live again.
    memories_restored: int = 0
    # Tombstoned memories of the context the deletion did not tombstone:
    # forgotten earlier, or archived/merged by Sleep. They stay deleted.
    memories_left_deleted: int = 0
    # Set when the context comes back under another name.
    renamed_from: str | None = None
    warnings: list[str] = field(default_factory=list)


def _deleted_by_the_deletion(context: Context, *, shared_timestamp: bool) -> Any:
    """The predicate for the memories ``context``'s deletion tombstoned.

    With ``shared_timestamp`` (a deletion from v0.90.0 on) only the memories
    stamped with the context's own ``deleted_at``; otherwise those up to
    ``DELETION_WINDOW`` before it.
    """
    assert context.deleted_at is not None
    if shared_timestamp:
        when = Memory.deleted_at == context.deleted_at
    else:
        when = and_(
            Memory.deleted_at <= context.deleted_at,
            Memory.deleted_at >= context.deleted_at - DELETION_WINDOW,
        )
    return and_(
        Memory.workspace_id == context.workspace_id,
        Memory.context_id == context.id,
        Memory.deleted_at.is_not(None),
        when,
        Memory.deleted_by.is_not_distinct_from(context.deleted_by),
    )


async def restore_deleted_context(
    db: AsyncSession,
    context_id: UUID,
    *,
    dry_run: bool = True,
    new_name: str | None = None,
    actor_id: str = "cli",
    actor_email: str | None = None,
) -> ContextRestoreResult:
    """Bring a soft-deleted context and the memories its deletion tombstoned back.

    Args:
        db: Async session. A dry run only reads; a restore commits.
        context_id: The deleted context.
        dry_run: Count only.
        new_name: Restore under this name, for when a live context of the
            same workspace has taken the old one.
        actor_id: Who restores, for the audit row and the log.
        actor_email: Shown in the audit row (defaults to ``actor_id``).

    Returns:
        ContextRestoreResult with the counts.

    Raises:
        NotFoundException: No context row with this id (a hard-deleted
            context; ``POST /admin/contexts/recover`` rebuilds one from
            surviving points, if any).
        ConflictError: The context is not deleted, its workspace is, or its
            name or ``resource_id`` is now used by a live context.
        ValidationError: ``new_name`` is not a valid context name.
    """
    if new_name is not None:
        if (
            len(new_name) > 100
            or not CONTEXT_NAME_PATTERN.match(new_name)
            or new_name == DEFAULT_CONTEXT_NAME
        ):
            raise ValidationError(
                "new_name must be 1-100 characters of a-z, 0-9, '-' or '_', and not 'default'",
                field="new_name",
            )

    query = select(Context).where(Context.id == context_id)
    if not dry_run:
        # Two restores of the same context must not both pass the checks.
        query = query.with_for_update()
    context = (await db.execute(query)).scalar_one_or_none()
    if context is None:
        raise NotFoundException("Context", str(context_id))
    if context.deleted_at is None:
        raise ConflictError(f"Context {context_id} is not deleted")
    workspace = (
        await db.execute(select(Workspace).where(Workspace.id == context.workspace_id))
    ).scalar_one()
    if workspace.deleted_at is not None:
        # Deleting a workspace is final: its points are gone and its members
        # were let go. A context cannot come back into it.
        raise ConflictError(f"The workspace of context {context_id} is deleted")

    name = new_name or context.name
    result = ContextRestoreResult(
        context_id=str(context.id),
        workspace_id=str(context.workspace_id),
        name=name,
        deleted_at=context.deleted_at,
        deleted_by=context.deleted_by,
        dry_run=dry_run,
        renamed_from=context.name if name != context.name else None,
    )

    taken = await db.execute(
        select(Context.id).where(
            Context.workspace_id == context.workspace_id,
            Context.name == name,
            Context.deleted_at.is_(None),
        )
    )
    if taken.first() is not None:
        raise ConflictError(
            f"A live context in this workspace is already named '{name}'; "
            "restore it under another name (new_name / --name)"
        )
    if context.resource_id:
        resource_taken = await db.execute(
            select(Context.id).where(
                Context.resource_id == context.resource_id,
                Context.deleted_at.is_(None),
            )
        )
        if resource_taken.first() is not None:
            raise ConflictError(
                f"A live context already serves resource '{context.resource_id}'; "
                "delete or unpublish it before restoring this one"
            )

    # A deletion from v0.90.0 on stamped its memories with the context's own
    # timestamp; when any memory carries it, the window (for older deletions)
    # is not used, so a memory forgotten just before the deletion stays deleted.
    shared_timestamp = (
        await db.execute(
            select(Memory.id)
            .where(
                Memory.workspace_id == context.workspace_id,
                Memory.context_id == context.id,
                Memory.deleted_at == context.deleted_at,
                Memory.deleted_by.is_not_distinct_from(context.deleted_by),
            )
            .limit(1)
        )
    ).first() is not None
    by_the_deletion = _deleted_by_the_deletion(context, shared_timestamp=shared_timestamp)
    counts = (
        await db.execute(
            select(
                func.count().filter(by_the_deletion),
                func.count(),
            ).where(
                Memory.context_id == context.id,
                Memory.deleted_at.is_not(None),
            )
        )
    ).one()
    result.memories_restored = int(counts[0])
    result.memories_left_deleted = int(counts[1]) - result.memories_restored
    if not shared_timestamp and result.memories_restored:
        # Either a deletion from before v0.90.0, or one of a context that had
        # no live memories left (nothing carries its timestamp then). The
        # rows cannot tell the two apart, so the admin is told what the
        # window takes.
        result.warnings.append(
            "No memory carries the context's deletion time (a deletion from before "
            f"v0.90.0, or of a context with no live memories): the "
            f"{result.memories_restored} memories its deleter deleted up to "
            f"{int(DELETION_WINDOW.total_seconds() // 60)} minutes before it are "
            "restored, including any forgotten on purpose in that time."
        )

    # The context cap is the plan's, for its users' own creates; an admin
    # restore is not refused by it (nor does it go through the quota gates,
    # which only create paths call — #1552), but says so.
    live_contexts = (
        await db.execute(
            select(func.count())
            .select_from(Context)
            .where(Context.workspace_id == context.workspace_id, Context.deleted_at.is_(None))
        )
    ).scalar_one()
    max_contexts = workspace.effective_max_contexts
    if live_contexts >= max_contexts:
        result.warnings.append(
            f"The workspace has {live_contexts} live context(s) and its plan allows "
            f"{max_contexts}; the restore puts it over the cap."
        )

    if dry_run:
        # Ends the read transaction; the CLI holds the session open while the
        # operator answers its prompt.
        await db.rollback()
        return result

    restored = await db.execute(
        update(Memory)
        .where(by_the_deletion)
        .values(
            deleted_at=None,
            deleted_by=None,
            embedding_status="pending",
            embedding_retry_count=0,
            embedding_error=None,
        )
        .execution_options(synchronize_session=False)
    )
    # Recounted from the UPDATE: a purge that ran since the count above
    # removed rows, and those cannot come back.
    result.memories_restored = int(cast(CursorResult[Any], restored).rowcount or 0)

    context.deleted_at = None
    context.deleted_by = None
    context.name = name
    db.add(
        AuditLog(
            user_email=actor_email or actor_id,
            user_id=actor_id,
            action=AUDIT_ACTION,
            resource=f"context:{context.id}",
            user_metadata={
                "workspace_id": result.workspace_id,
                "name": name,
                "renamed_from": result.renamed_from,
                "deleted_at": to_utc_iso(result.deleted_at),
                "deleted_by": result.deleted_by,
                "memories_restored": result.memories_restored,
                "memories_left_deleted": result.memories_left_deleted,
            },
        )
    )
    try:
        await db.commit()
    except IntegrityError as e:
        # A context took the name (or the resource) after the check above.
        await db.rollback()
        raise ConflictError(
            f"Context {context_id} could not be restored: its name or resource is now taken"
        ) from e

    logger.info(
        "context_restored",
        context_id=result.context_id,
        workspace_id=result.workspace_id,
        name=name,
        renamed_from=result.renamed_from,
        memories_restored=result.memories_restored,
        memories_left_deleted=result.memories_left_deleted,
        actor_id=actor_id,
    )
    return result
