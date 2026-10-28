"""Shared administrative/CLI deletion, preserving explicitly transferred data.

The caller owns the transaction. Validate every successor before changing any
ownership; lock users, identity links, workspaces, contexts, then memories.
"""

from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole, reaches_context
from models.auth import (
    APIKey,
    Context,
    ContextMember,
    OAuth2Client,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from services.connector_key_ownership import reown_connector_keys
from services.erasure_details import locked_ids
from services.file_storage_service import FileStorageService
from services.identity_link_service import hand_over_private_contexts, lock_identity_links
from services.workspace_ownership_service import WorkspaceOwnershipService


@dataclass
class UserDeletionResult:
    """Committed by the caller; storage release and email follow that commit."""

    memories: int = 0
    api_keys: int = 0
    connector_api_keys: int = 0
    released_bytes: dict[UUID, int] = field(default_factory=dict)


async def lock_deletion_users(db: AsyncSession, user_id: str) -> set[str]:
    """Lock the subject and existing context successors before any child rows."""
    owners = (
        select(Workspace.owner_user_id)
        .join(Context, Context.workspace_id == Workspace.id)
        .where(Context.created_by == user_id)
    )
    locked = set(
        (
            await db.execute(
                select(User.user_id)
                .where(or_(User.user_id == user_id, User.user_id.in_(owners)))
                .order_by(User.user_id)
                .with_for_update()
            )
        ).scalars()
    )
    await lock_identity_links(db)
    current_owners = set((await db.execute(owners)).scalars())
    if user_id not in locked or not current_owners <= locked:
        raise HTTPException(409, "Context ownership changed; retry deletion")
    return locked


async def transfer_user_ownership(
    db: AsyncSession,
    user_id: str,
    *,
    workspace_transfers: dict[UUID, str],
    context_transfers: dict[UUID, str],
    actor_email: str,
    actor_user_id: str,
) -> dict[UUID, str]:
    """Require an explicit successor for each owned workspace/created context.

    Workspace selections also supply the default successor for contexts in that
    workspace. A context-specific selection overrides that default. Soft-deleted
    owned workspaces must be restored/transferred separately (#2406).
    """
    successors = set(workspace_transfers.values()) | set(context_transfers.values())
    users = set(
        (
            await db.execute(
                select(User.user_id)
                .where(User.user_id.in_(successors | {user_id}))
                .order_by(User.user_id)
                .with_for_update()
            )
        ).scalars()
    )
    if user_id not in users:
        raise HTTPException(404, "User not found")
    if user_id in successors or not successors <= users:
        raise HTTPException(409, "Transfer targets must be surviving users")
    await lock_identity_links(db)
    workspaces = list(
        (
            await db.execute(
                select(Workspace)
                .where(Workspace.owner_user_id == user_id)
                .order_by(Workspace.id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    contexts = list(
        (
            await db.execute(
                select(Context)
                .where(Context.created_by == user_id)
                .order_by(Context.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    context_ids = {c.id for c in contexts}
    workspace_ids = {w.id for w in workspaces} | {c.workspace_id for c in contexts}
    if set(context_transfers) - context_ids or set(workspace_transfers) - workspace_ids:
        raise HTTPException(409, "Transfer selections no longer match the user's resources")
    missing_ws = [str(w.id) for w in workspaces if w.id not in workspace_transfers]
    missing_ctx = [
        str(c.id)
        for c in contexts
        if c.id not in context_transfers and c.workspace_id not in workspace_transfers
    ]
    if missing_ws or missing_ctx:
        raise HTTPException(
            409,
            {
                "message": "Ownership transfer required",
                "workspace_ids": missing_ws,
                "context_ids": missing_ctx,
            },
        )
    if any(w.deleted_at is not None for w in workspaces):
        raise HTTPException(409, "Restore and transfer soft-deleted owned workspaces first")

    members = {
        (m.workspace_id, m.user_id): m
        for m in (
            await db.execute(
                select(WorkspaceMember)
                .where(
                    WorkspaceMember.user_id.in_(successors),
                    WorkspaceMember.workspace_id.in_(workspace_ids),
                )
                .order_by(WorkspaceMember.id)
                .with_for_update()
            )
        ).scalars()
    }
    for workspace in workspaces:
        if (workspace.id, workspace_transfers[workspace.id]) not in members:
            raise HTTPException(409, "Workspace successor must be an existing member")
    for context in contexts:
        successor = context_transfers.get(context.id) or workspace_transfers[context.workspace_id]
        member = members.get((context.workspace_id, successor))
        becoming_owner = workspace_transfers.get(context.workspace_id) == successor and any(
            w.id == context.workspace_id for w in workspaces
        )
        if member is None or (
            not becoming_owner
            and (
                member.role == WorkspaceRole.VIEWER
                or not reaches_context(member.role, member.allowed_context_ids, context.id)
            )
        ):
            raise HTTPException(409, "Context successor must have workspace/context access")

    # Validation finished. Keep all transfers in the deletion transaction.
    changed: dict[UUID, str] = {}
    for workspace in workspaces:
        successor = workspace_transfers[workspace.id]
        await WorkspaceOwnershipService(db).transfer_ownership(
            workspace_id=workspace.id,
            current_owner_id=user_id,
            target_user_id=successor,
            performed_by_email=actor_email,
            performed_by_user_id=actor_user_id,
            commit=False,
        )
        changed[workspace.id] = successor
    for context in contexts:
        context.created_by = (
            context_transfers.get(context.id) or workspace_transfers[context.workspace_id]
        )
    await db.flush()
    return changed


async def delete_user_rows(
    db: AsyncSession,
    user_id: str,
    *,
    delete_workspace_ids: list[UUID] | None = None,
    preserve_created_contexts: bool = False,
) -> UserDeletionResult:
    """Delete personal rows; preserve linked contexts and purge doomed files.

    Both entry points lock the subject before invoking this. No commit or external
    I/O is performed here. API callers have already transferred created contexts;
    the CLI may still delete untransferred contexts and sole-owned workspaces.
    """
    workspace_ids = delete_workspace_ids or []
    await db.execute(select(User.user_id).where(User.user_id == user_id).with_for_update())
    await lock_identity_links(db)
    if workspace_ids:
        # Block FK inserts of new contexts before collecting the cascade union.
        # NOWAIT also avoids workspace-delete -> user-preference lock crossings.
        await db.execute(
            select(Workspace.id)
            .where(Workspace.id.in_(workspace_ids))
            .order_by(Workspace.id)
            .with_for_update(nowait=True)
        )
    if not preserve_created_contexts:
        remaining = await db.scalar(
            select(Context.id).where(Context.created_by == user_id).limit(1)
        )
        owned = await db.scalar(
            select(Workspace.id).where(Workspace.owner_user_id == user_id).limit(1)
        )
        if remaining is not None or owned is not None:
            raise HTTPException(409, "Ownership changed; select successors and retry deletion")
    await db.execute(
        locked_ids(
            Context, lambda c: (or_(c.created_by == user_id, c.workspace_id.in_(workspace_ids)),)
        )
    )
    # Lock the complete memory set before hand-over can delete a subset.
    await db.execute(
        locked_ids(
            Memory,
            lambda m: (
                or_(
                    m.user_id == user_id,
                    m.context_id.in_(
                        select(Context.id).where(
                            or_(
                                Context.created_by == user_id,
                                Context.workspace_id.in_(workspace_ids),
                            )
                        )
                    ),
                ),
            ),
        )
    )
    result = UserDeletionResult()
    # Include the rows hand_over_private_contexts deletes in the reported count.
    result.memories = (
        await db.execute(select(func.count(Memory.id)).where(Memory.user_id == user_id))
    ).scalar_one()
    await hand_over_private_contexts(db, user_id)
    if preserve_created_contexts:
        # The CLI's surviving contexts follow their workspace's surviving
        # owner; the explicit API path has already selected its successors.
        rows = (
            await db.execute(
                select(Context, Workspace.owner_user_id)
                .join(Workspace, Workspace.id == Context.workspace_id)
                .where(
                    Context.created_by == user_id,
                    Workspace.owner_user_id != user_id,
                    Workspace.id.not_in(workspace_ids),
                )
            )
        ).all()
        for context, owner in rows:
            context.created_by = owner
        await db.flush()
    result.connector_api_keys = await reown_connector_keys(db, user_id)
    await db.execute(
        delete(Memory).where(Memory.id.in_(locked_ids(Memory, lambda m: (m.user_id == user_id,))))
    )
    keys = await db.execute(
        delete(APIKey).where(APIKey.id.in_(locked_ids(APIKey, lambda k: (k.user_id == user_id,))))
    )
    result.api_keys = cast(CursorResult[Any], keys).rowcount or 0
    await db.execute(delete(OAuth2Client).where(OAuth2Client.owner_id == user_id))
    ctx_ids = list(
        (
            await db.execute(
                locked_ids(
                    Context,
                    lambda c: (or_(c.created_by == user_id, c.workspace_id.in_(workspace_ids)),),
                )
            )
        ).scalars()
    )
    result.released_bytes = await FileStorageService(db).purge_files_for_contexts(ctx_ids)
    if ctx_ids:
        await db.execute(delete(Context).where(Context.id.in_(ctx_ids)))
    await db.execute(delete(ContextMember).where(ContextMember.user_id == user_id))
    await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == user_id))
    if workspace_ids:
        await db.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
    await db.execute(delete(User).where(User.user_id == user_id))
    return result


async def finish_user_deletion(result: UserDeletionResult) -> None:
    """Release cached file quota after the deletion transaction commits."""
    from services import storage_quota_service

    for workspace_id, size_bytes in result.released_bytes.items():
        await storage_quota_service.release_storage_bytes(
            workspace_id=workspace_id, size_bytes=size_bytes
        )
