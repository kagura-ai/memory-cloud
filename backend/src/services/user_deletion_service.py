"""Shared administrative/CLI deletion, preserving explicitly transferred data.

The caller owns the transaction. Validate every successor before changing any
ownership; lock users, identity links, workspaces, contexts, then memories.

A private context is readable by its creator (and the accounts linked to it)
only. One nobody else wrote in is the creator's own: it is never handed to a
successor the admin picks, and is deleted with the user, files included (the
admin delete has always removed what it does not hand over; account erasure
keeps such a context under a pseudonymous creator instead). Before that, a
linked account that wrote in a private context takes it over
(``hand_over_private_contexts``, #1784). A private context someone else wrote
in that the hand-over did not take (no eligible linked author, or the context
is soft-deleted), and a connector's write target (connectors create theirs
private; it is the workspace's), keep another account's data, so they take a
successor like a shared context: the API requires one, the CLI hands them to
the surviving workspace owner (#2417).

A soft-deleted workspace the user owns goes with the user on both paths
(#2406): its owner already deleted it, so it is never handed to a successor.
It is hard-deleted like a live workspace only the user is in (the CLI's
#2372 cascade): its contexts and their files first (their memories cascade
from the contexts), its memories with no context, then the workspace row,
whose foreign keys cascade to its keys, members, invitations and file rows
(the #2395 trigger queues the stored objects). One
``workspace_purged`` audit row per hard-deleted workspace commits with it.
Its vector points went when it was soft-deleted; ``sweep_orphan_vectors``
removes any that call left (``finish_user_deletion`` deletes the points of
the other contexts deleted here after the commit).
"""

from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from auth.workspace_roles import WorkspaceRole, reaches_context
from models.auth import (
    APIKey,
    AuditLog,
    Context,
    ContextMember,
    OAuth2Client,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from models.resource import WorkspaceConnector
from services.billing_contract import ensure_no_billing_contract, workspaces_under_contract
from services.connector_key_ownership import reown_connector_keys
from services.erasure_details import locked_ids
from services.file_storage_service import FileStorageService
from services.identity_link_service import (
    hand_over_private_contexts,
    lock_identity_links,
    predict_private_context_takeovers,
)
from services.workspace_ownership_service import WorkspaceOwnershipService
from utils.logger import get_logger

logger = get_logger(__name__)

WORKSPACE_PURGED_ACTION = "workspace_purged"

PRIVATE_CONTEXT_NOT_TRANSFERABLE = (
    "A private context nobody else wrote in cannot be transferred: it is deleted with the user"
)
PRIVATE_CONTEXT_HANDED_OVER = (
    "A private context a linked account wrote in cannot be transferred: that account takes it over"
)

# The write targets of the workspace connectors: a context here belongs to the
# workspace whatever its privacy flag says.
_CONNECTOR_CONTEXT_IDS = select(WorkspaceConnector.context_id).where(
    WorkspaceConnector.context_id.is_not(None)
)


def _contextless_in(m: Any, workspaces: list[UUID] | Any) -> ColumnElement[bool]:
    """``m`` is a memory row with no context in one of ``workspaces`` (a list
    of ids, or a workspace id column to correlate with): no foreign key takes
    it with its workspace."""
    in_workspace = (
        m.workspace_id.in_(workspaces)
        if isinstance(workspaces, list)
        else m.workspace_id == workspaces
    )
    return and_(in_workspace, m.context_id.is_(None))


def _personal(c: Any, user_id: str) -> ColumnElement[bool]:
    """``c`` is a private context of ``user_id``'s own: created by it, written
    in by nobody else, and no connector's write target. Such a context goes
    with the user; every other one it created takes a successor.

    "Written in by nobody else" counts another account's rows live or
    soft-deleted: deleting the context would cascade over a row its author
    can still restore. (The hand-over takes a context for a linked account
    with live rows; one with only soft-deleted ones takes a successor.)
    """
    # A fresh alias: the enclosing query may already select from memories,
    # and auto-correlation would otherwise empty this FROM.
    memory = aliased(Memory)
    others_wrote = (
        select(memory.id).where(memory.context_id == c.id, memory.user_id != user_id).exists()
    )
    return and_(
        c.created_by == user_id,
        c.is_private.is_(True),
        c.id.not_in(_CONNECTOR_CONTEXT_IDS),
        ~others_wrote,
    )


@dataclass(frozen=True)
class PurgedWorkspace:
    """A workspace hard-deleted with its owner, and how much went with it
    (every context and memory row in it, soft-deleted ones included)."""

    id: UUID
    name: str
    soft_deleted: bool
    contexts: int
    memories: int

    def summary(self) -> dict[str, Any]:
        """The API response entry (#2406)."""
        return {
            "id": str(self.id),
            "name": self.name,
            "contexts": self.contexts,
            "memories": self.memories,
        }


@dataclass
class UserDeletionResult:
    """Committed by the caller; storage release, vector cleanup and email
    follow that commit."""

    memories: int = 0
    api_keys: int = 0
    connector_api_keys: int = 0
    contexts: int = 0
    released_bytes: dict[UUID, int] = field(default_factory=dict)
    purged_workspaces: list[PurgedWorkspace] = field(default_factory=list)
    # (workspace_id, context_id) of every context deleted, for the points.
    deleted_contexts: list[tuple[UUID, UUID]] = field(default_factory=list)


async def workspace_purge_counts(
    db: AsyncSession, *where: ColumnElement[bool]
) -> list[PurgedWorkspace]:
    """The workspaces matching ``where``, with their context and memory counts."""
    contexts = (
        select(func.count(Context.id))
        .where(Context.workspace_id == Workspace.id)
        .correlate(Workspace)
        .scalar_subquery()
    )
    memory = aliased(Memory)
    context = aliased(Context)
    # What the purge deletes: the memories of the workspace's contexts
    # (whatever their own workspace_id says) and its context-less ones. Two
    # counts, each correlated to the outer ``workspaces`` row and each able
    # to use an index. (A select nested inside one of them would need its own
    # ``.correlate(Workspace)``; without it ``workspaces`` joins its FROM and
    # every workspace's contexts match.)
    in_contexts = (
        select(func.count(memory.id))
        .join(context, context.id == memory.context_id)
        .where(context.workspace_id == Workspace.id)
        .correlate(Workspace)
        .scalar_subquery()
    )
    contextless = (
        select(func.count(memory.id))
        .where(_contextless_in(memory, Workspace.id))
        .correlate(Workspace)
        .scalar_subquery()
    )
    memories = in_contexts + contextless
    rows = await db.execute(
        select(Workspace.id, Workspace.name, Workspace.deleted_at, contexts, memories)
        .where(*where)
        .order_by(Workspace.id)
    )
    return [
        PurgedWorkspace(ws_id, name, deleted_at is not None, n_contexts, n_memories)
        for ws_id, name, deleted_at, n_contexts, n_memories in rows.all()
    ]


async def soft_deleted_owned_workspaces(db: AsyncSession, user_id: str) -> list[PurgedWorkspace]:
    """The soft-deleted workspaces ``user_id`` owns: deleting the user
    hard-deletes them (#2406)."""
    return await workspace_purge_counts(
        db, Workspace.owner_user_id == user_id, Workspace.deleted_at.is_not(None)
    )


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
    """Require an explicit successor for each owned workspace and shared context.

    Workspace selections also supply the default successor for contexts in that
    workspace. A context-specific selection overrides that default. A
    soft-deleted owned workspace is hard-deleted with the user (#2406): it and
    the contexts in it take no successor, and a selection naming one is
    refused as no longer matching. A private
    context of the user's own is not transferable (see the module docstring); a
    selection naming one is refused. An owned workspace under a running
    subscription refuses the whole deletion (``BILLING-005``): the subscription
    is the user's, so it ends first, as account erasure and ``cli.delete_admin``
    require (#1940).
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
                .where(Workspace.owner_user_id == user_id, Workspace.deleted_at.is_(None))
                .order_by(Workspace.id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    ensure_no_billing_contract(workspaces)
    # The soft-deleted ones go with the user, everything in them included
    # (``delete_user_rows`` locks them). With the live ones locked, none can
    # join this set before that.
    purged = set(
        (
            await db.execute(
                select(Workspace.id).where(
                    Workspace.owner_user_id == user_id, Workspace.deleted_at.is_not(None)
                )
            )
        ).scalars()
    )
    created = list(
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
    # #1784 decides first which private contexts a linked account takes over
    # (``delete_user_rows`` applies it after this validation, under the memory
    # locks); of the rest, one of the user's own goes with it and everything
    # else needs a successor. Read-only here: the common 409 must not lock or
    # write more than the rows above.
    created = [c for c in created if c.workspace_id not in purged]
    taken = await predict_private_context_takeovers(db, user_id, created)
    personal = set(
        (await db.execute(select(Context.id).where(_personal(Context, user_id)))).scalars()
    ) - set(taken)
    if context_transfers.keys() & taken.keys():
        raise HTTPException(409, PRIVATE_CONTEXT_HANDED_OVER)
    if context_transfers.keys() & personal:
        raise HTTPException(409, PRIVATE_CONTEXT_NOT_TRANSFERABLE)
    contexts = [c for c in created if c.id not in taken and c.id not in personal]
    context_ids = {c.id for c in contexts}
    # A workspace selection is the default for the contexts created there, so
    # one naming any workspace the user created in is well-formed, even when
    # nothing there needs a successor any more.
    workspace_ids = {w.id for w in workspaces} | {c.workspace_id for c in created}
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
    actor_email: str = "cli@local",
    actor_user_id: str | None = None,
) -> UserDeletionResult:
    """Delete personal rows; preserve linked contexts and purge doomed files.

    Both entry points lock the subject before invoking this. No commit or external
    I/O is performed here. API callers have already transferred the shared contexts
    the user created; the CLI hands those in surviving workspaces to the workspace
    owner and deletes sole-owned workspaces. Both hard-delete the soft-deleted
    workspaces the user owns (#2406), and a private context of the user's own (see
    the module docstring) is deleted on both paths, with its files, wherever it is.

    Args:
        actor_email: The audit actor of each ``workspace_purged`` row.
        actor_user_id: Its user_id (default: the deleted user, as the CLI's
            ownership-transfer audit does).
    """
    workspace_ids: list[UUID] = sorted(set(delete_workspace_ids or []))

    def doomed(c: Any) -> tuple[ColumnElement[bool]]:
        """The contexts that go with the user: the private ones of its own
        (wherever they are: nobody could read one once its creator is gone),
        plus everything in the workspaces being deleted."""
        return (or_(_personal(c, user_id), c.workspace_id.in_(workspace_ids)),)

    await db.execute(select(User.user_id).where(User.user_id == user_id).with_for_update())
    await lock_identity_links(db)
    # Block FK inserts of new contexts before collecting the cascade union.
    # NOWAIT also avoids workspace-delete -> user-preference lock crossings.
    # The live workspaces the user still owns are locked too, so none can be
    # soft-deleted (which takes the same row lock) after the read below: the
    # soft-deleted set read next is final.
    await db.execute(
        select(Workspace.id)
        .where(
            or_(
                Workspace.id.in_(workspace_ids),
                and_(Workspace.owner_user_id == user_id, Workspace.deleted_at.is_(None)),
            )
        )
        .order_by(Workspace.id)
        .with_for_update(nowait=True)
    )
    # The soft-deleted ones (#2406) wait instead: no owner mutation reaches
    # them (``lock_workspace_for_update`` takes live rows only), and the
    # deleted-workspace file repair (``purge_deleted_workspace_files``) locks
    # one at a time with SKIP LOCKED and no user lock, so waiting cannot
    # deadlock, where a NOWAIT would fail a CLI run after its transfers had
    # committed.
    soft_deleted = set(
        (
            await db.execute(
                select(Workspace.id)
                .where(Workspace.owner_user_id == user_id, Workspace.deleted_at.is_not(None))
                .order_by(Workspace.id)
                .with_for_update()
            )
        ).scalars()
    )
    workspace_ids = sorted(set(workspace_ids) | soft_deleted)
    deleted_workspaces = set(workspace_ids)
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
                    _contextless_in(m, workspace_ids),
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
    if workspace_ids:
        # Counted before anything in them is deleted, for the audit rows.
        result.purged_workspaces = await workspace_purge_counts(db, Workspace.id.in_(workspace_ids))
    # Include the rows hand_over_private_contexts deletes in the reported count.
    result.memories = (
        await db.execute(select(func.count(Memory.id)).where(Memory.user_id == user_id))
    ).scalar_one()
    await hand_over_private_contexts(db, user_id)
    if not preserve_created_contexts:
        # The API path validated with the same rule the hand-over just applied;
        # anything else still created by the user slipped in since.
        remaining = await db.scalar(
            select(Context.id)
            .where(
                Context.created_by == user_id,
                ~_personal(Context, user_id),
                Context.workspace_id.not_in(workspace_ids),
            )
            .limit(1)
        )
        owned = await db.scalar(
            select(Workspace.id)
            .where(Workspace.owner_user_id == user_id, Workspace.id.not_in(workspace_ids))
            .limit(1)
        )
        if remaining is not None or owned is not None:
            raise HTTPException(409, "Ownership changed; select successors and retry deletion")
    if preserve_created_contexts:
        # The CLI's surviving shared (and connector) contexts follow their
        # workspace's surviving owner; the API path has selected successors.
        rows = (
            await db.execute(
                select(Context, Workspace.owner_user_id)
                .join(Workspace, Workspace.id == Context.workspace_id)
                .where(
                    Context.created_by == user_id,
                    ~_personal(Context, user_id),
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
    ctx_ids = list((await db.execute(locked_ids(Context, doomed))).scalars())
    result.contexts = len(ctx_ids)
    if ctx_ids:
        # A soft-deleted workspace's points went when it was deleted
        # (``WorkspaceService.delete_workspace``); ``sweep_orphan_vectors``
        # takes any that call left, so only the other contexts are asked.
        result.deleted_contexts = [
            (ws_id, ctx_id)
            for ws_id, ctx_id in (
                await db.execute(
                    select(Context.workspace_id, Context.id)
                    .where(Context.id.in_(ctx_ids), Context.workspace_id.not_in(list(soft_deleted)))
                    .order_by(Context.id)
                )
            ).all()
        ]
    released = await FileStorageService(db).purge_files_for_contexts(ctx_ids)
    # Redis follows the committed state: only a workspace that survives this
    # deletion keeps a quota counter to release.
    result.released_bytes = {
        ws_id: size for ws_id, size in released.items() if ws_id not in deleted_workspaces
    }
    if ctx_ids:
        await db.execute(delete(Context).where(Context.id.in_(ctx_ids)))
    await db.execute(delete(ContextMember).where(ContextMember.user_id == user_id))
    await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == user_id))
    if workspace_ids:
        # ``memories.workspace_id`` has no foreign key: a row with no context
        # would outlive its workspace.
        await db.execute(
            delete(Memory).where(
                Memory.id.in_(locked_ids(Memory, lambda m: (_contextless_in(m, workspace_ids),)))
            )
        )
        # The callers refuse a live workspace under a running subscription;
        # a soft-deleted one never blocks (#1940), but one deleted before that
        # rule may still be charged, so its purge says so, as the erasure
        # override log does.
        under_contract = {
            ws.id
            for ws in workspaces_under_contract(
                list(
                    (
                        await db.execute(select(Workspace).where(Workspace.id.in_(workspace_ids)))
                    ).scalars()
                ),
                include_deleted=True,
            )
        }
        if under_contract:
            logger.warning(
                "user_deletion_purged_workspaces_under_contract",
                workspace_ids=sorted(str(ws_id) for ws_id in under_contract),
            )
        # The audit trail commits (or rolls back) with the purge itself.
        db.add_all(
            [
                AuditLog(
                    user_email=actor_email,
                    user_id=actor_user_id or user_id,
                    action=WORKSPACE_PURGED_ACTION,
                    resource=f"workspace:{ws.id}",
                    user_metadata={
                        "previous_owner_id": user_id,
                        "soft_deleted": ws.soft_deleted,
                        "contexts": ws.contexts,
                        "memories": ws.memories,
                        "billing_contract_active": ws.id in under_contract,
                    },
                )
                for ws in result.purged_workspaces
            ]
        )
        await db.flush()
        await db.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
    await db.execute(delete(User).where(User.user_id == user_id))
    return result


async def finish_user_deletion(result: UserDeletionResult) -> None:
    """After the deletion transaction commits: release cached file quota,
    then delete the deleted contexts' vector points.

    The points are best-effort, as for ``WorkspaceService.delete_workspace``:
    Postgres no longer has the rows, so no reader can reach them, and the
    daily ``sweep_orphan_vectors`` removes any a failed call leaves.
    """
    from db.qdrant import delete_context_points_everywhere, list_memory_collections
    from services import storage_quota_service

    for workspace_id, size_bytes in result.released_bytes.items():
        await storage_quota_service.release_storage_bytes(
            workspace_id=workspace_id, size_bytes=size_bytes
        )
    if not result.deleted_contexts:
        return
    collections: list[str] | None = None
    try:
        collections = await list_memory_collections()
    except Exception as exc:  # noqa: BLE001 - best effort: each context retries the listing
        logger.warning("user_deletion_list_collections_failed", error_class=type(exc).__name__)
    for workspace_id, context_id in result.deleted_contexts:
        try:
            await delete_context_points_everywhere(
                str(workspace_id), str(context_id), collections=collections
            )
        except Exception as exc:  # noqa: BLE001 - the orphan sweep removes what is left
            logger.warning(
                "user_deletion_context_points_failed",
                workspace_id=str(workspace_id),
                context_id=str(context_id),
                error_class=type(exc).__name__,
            )
