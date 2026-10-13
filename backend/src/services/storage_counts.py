"""The one definition of a workspace's reducible file storage (#1961).

Two gates compare a workspace's files against a plan's ``storage_limit_bytes``
and both must see the same number: the capacity-over lock (#1941, lifted by
deleting files) and the downgrade eligibility read the billing side uses
(#1123, "could this workspace fit the lower tier after cleanup"). The figure
is **the bytes the owner can still delete**: uploaded, not soft-deleted files
that are either workspace-scoped (``context_id IS NULL``) or bound to a live
context — the owner's view of ``FileStorageService.list_files`` and the storage
page (a member sees the subset of contexts they can access). Files left behind
in a soft-deleted context (``delete_context`` leaves them until #1960 purges
them with the context) are not
listed anywhere in the UI, so counting them would describe a lock the owner
cannot lift from the product and an eligibility they cannot earn.

This is deliberately *not* the upload quota's figure. The upload quota
(``storage_quota_service``, the ``workspace_storage_usage`` counter) bounds the
bytes that exist in R2, hidden files included — a different question, left
alone. Before this module the eligibility check summed every live file and so
disagreed with the lock whenever a deleted context still held files.

A leaf module, like ``memory_counts``: models and SQLAlchemy only.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context
from models.file_objects import FileObject


def reducible_storage_bytes_stmt(workspace_id: UUID) -> Select[int]:
    """``SELECT`` the bytes of ``workspace_id``'s files the owner can still delete.

    Args:
        workspace_id: The workspace whose files are summed.

    Returns:
        A scalar ``SELECT`` of the summed ``size_bytes`` (``0`` when none).
    """
    live_contexts = select(Context.id).where(
        Context.workspace_id == workspace_id,
        Context.deleted_at.is_(None),
    )
    return select(func.coalesce(func.sum(FileObject.size_bytes), 0)).where(
        FileObject.workspace_id == workspace_id,
        FileObject.status == "uploaded",
        FileObject.deleted_at.is_(None),
        or_(FileObject.context_id.is_(None), FileObject.context_id.in_(live_contexts)),
    )


async def reducible_storage_bytes(db: AsyncSession, workspace_id: UUID) -> int:
    """Run :func:`reducible_storage_bytes_stmt` and return the total.

    Args:
        db: Async session.
        workspace_id: The workspace whose files are summed.

    Returns:
        The summed bytes; ``0`` when the workspace has no such file.
    """
    return int(await db.scalar(reducible_storage_bytes_stmt(workspace_id)) or 0)
