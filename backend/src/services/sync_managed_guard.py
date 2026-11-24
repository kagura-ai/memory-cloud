"""Transaction fences shared by every ordinary writer and feed mode transition."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from config.settings import get_settings
from models.memory import Memory
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from models.sync_installation import SyncInstallation
from models.sync_management import SyncManagedTarget
from utils.exceptions import ConflictError, ManagedDataLockedError, SyncLockBusyError

_SHARED = text("SELECT pg_try_advisory_xact_lock_shared(hashtextextended(:key, 0))")
_EXCLUSIVE = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))")


async def management_modes(db: AsyncSession, context_ids: list[UUID]) -> dict[UUID, str]:
    """Display metadata only; this snapshot never authorizes a later write."""
    if not context_ids:
        return {}
    result = dict(
        (
            await db.execute(
                select(SyncManagedTarget.context_id, SyncManagedTarget.mode).where(
                    SyncManagedTarget.context_id.in_(context_ids)
                )
            )
        ).all()
    )
    if get_settings().sync_managed_enabled:
        legacy = (
            await db.scalars(
                select(SyncInstallation.context_id).where(
                    SyncInstallation.context_id.in_(context_ids)
                )
            )
        ).all()
        for context_id in legacy:
            result.setdefault(context_id, "feed")
    return result


async def live_public_endpoint_context_ids(db: AsyncSession, context_ids: list[UUID]) -> set[UUID]:
    """The contexts among ``context_ids`` that back a live public MCP endpoint (#2995).

    Display metadata only, like ``management_modes``: one lookup on the
    partial unique index of live endpoints, no public id and no text.
    """
    if not context_ids:
        return set()
    return set(
        (
            await db.scalars(
                select(PublicMcpEndpoint.context_id).where(
                    PublicMcpEndpoint.context_id.in_(context_ids),
                    PublicMcpEndpoint.revoked_at.is_(None),
                )
            )
        ).all()
    )


@dataclass
class TargetFence:
    """A transaction owns this context fence even when no mode row exists yet."""

    transaction: AsyncSessionTransaction | None
    mode: SyncManagedTarget | None
    legacy: SyncInstallation | None
    nested_transaction: AsyncSessionTransaction | None = None

    @property
    def state(self) -> str | None:
        """Existing persistent ownership survives disabling the rollout switch."""
        return self.mode.mode if self.mode is not None else "feed" if self.legacy else None


async def target_fence(
    db: AsyncSession, context_id: UUID, *, exclusive: bool = False
) -> TargetFence:
    """Nonblocking, transaction-scoped locks close both present- and absent-row races."""
    cache = db.info.setdefault("sync_managed_fences", {})
    cached = cache.get(context_id)
    if (
        not exclusive
        and isinstance(cached, TargetFence)
        and cached.transaction is db.get_transaction()
        and cached.nested_transaction is db.get_nested_transaction()
    ):
        return cached
    with db.no_autoflush:
        acquired = await db.scalar(
            _EXCLUSIVE if exclusive else _SHARED,
            {"key": "sync-management:" + str(context_id)},
        )
        if acquired is False:
            # Busy, not refused: the resource indexer retries the event later.
            raise SyncLockBusyError("Feed management is changing; retry")
        row = await db.scalar(
            select(SyncManagedTarget)
            .where(SyncManagedTarget.context_id == context_id)
            .execution_options(populate_existing=True)
        )
        mode = row if isinstance(row, SyncManagedTarget) else None
        legacy = None
        if mode is None and get_settings().sync_managed_enabled:
            row = await db.scalar(
                select(SyncInstallation).where(SyncInstallation.context_id == context_id)
            )
            legacy = row if isinstance(row, SyncInstallation) else None
        result = TargetFence(db.get_transaction(), mode, legacy, db.get_nested_transaction())
    cache[context_id] = result
    return result


async def assert_context_mutable(db: AsyncSession, context_id: UUID | str | None) -> None:
    """An ordinary content/identity writer can only mutate an unmanaged/manual target."""
    if context_id is None:
        return
    parsed = context_id if isinstance(context_id, UUID) else UUID(str(context_id))
    fence = await target_fence(db, parsed)
    if fence.state not in (None, "manual"):
        raise ManagedDataLockedError(fence.state)


async def assert_memory_mutable(db: AsyncSession, memory: Memory) -> None:
    """Fence the persisted source and destination before an in-place/move write."""
    contexts = {memory.context_id} if memory.context_id is not None else set()
    if memory.id is not None:
        with db.no_autoflush:
            old = await db.scalar(select(Memory.context_id).where(Memory.id == memory.id))
        if isinstance(old, UUID):
            contexts.add(old)
    for context_id in sorted(contexts, key=str):
        await assert_context_mutable(db, context_id)


async def assert_vector_memory_mutable(
    db: AsyncSession, memory_id: UUID, *, rebuild_from: Memory | None = None
) -> None:
    """Background vector writers resolve the authoritative current context first.

    On a ``feed`` / ``stopping`` target every vector write is refused, except a
    restore-only rebuild (#2908): the caller passes the row it rebuilds the
    point from as ``rebuild_from``, and the write passes when that row is a
    live feed row whose stored and in-hand content and resource version equal
    its canonical sync record (``_canonical_feed_row``). The test runs after
    the shared fence is taken, in the same transaction, so no mode transition
    can come between it and the caller's commit. A rebuild also fences the
    in-hand row's context when it differs from the stored one.

    Raises:
        ManagedDataLockedError: The row's context is feed-managed and this is
            not a rebuild of its own unchanged canonical content.
        ConflictError: A mode transition holds the fence; retry.
    """
    context_id = await db.scalar(select(Memory.context_id).where(Memory.id == memory_id))
    contexts = {context_id} if isinstance(context_id, UUID) else set()
    if rebuild_from is not None and isinstance(rebuild_from.context_id, UUID):
        contexts.add(rebuild_from.context_id)
    for context in sorted(contexts, key=str):
        fence = await target_fence(db, context)
        if fence.state in (None, "manual"):
            continue
        if (
            rebuild_from is not None
            and rebuild_from.id == memory_id
            and await _canonical_feed_row(db, fence, rebuild_from)
        ):
            continue
        raise ManagedDataLockedError(fence.state)


async def _canonical_feed_row(db: AsyncSession, fence: TargetFence, memory: Memory) -> bool:
    """Whether ``memory`` holds exactly its canonical record under the enrolled target.

    The conditions ``canonical_index_complete`` checks for one row, minus the
    embedding status: live, in the target's context and resource, a feed row
    (``source`` ``resource_ingest``, ``version`` 1, authored by the context
    creator) whose document has a canonical record with a payload, whose
    resource version is the record's and whose content is the record's
    payload. The in-hand row must carry the same values, since a point is built
    from it. A retained target not enrolled yet has no target row: refused.
    """
    from sqlalchemy import and_, case, cast, func
    from sqlalchemy.dialects.postgresql import JSONB

    from models.auth import Context
    from models.sync_feed import SyncFeed, SyncRecord

    target = fence.mode
    if target is None:
        return False
    stored = case(
        (func.pg_input_is_valid(Memory.content, "jsonb"), cast(Memory.content, JSONB)),
        else_=None,
    )
    with db.no_autoflush:
        row = (
            await db.execute(
                select(
                    Memory.content,
                    Memory.context_id,
                    Memory.user_id,
                    Memory.resource_id,
                    Memory.resource_doc_id,
                    Memory.resource_version,
                )
                .join(
                    SyncInstallation,
                    and_(
                        SyncInstallation.id == target.installation_id,
                        SyncInstallation.context_id == Memory.context_id,
                        SyncInstallation.resource_id == Memory.resource_id,
                    ),
                )
                .join(
                    Context,
                    and_(Context.id == Memory.context_id, Context.created_by == Memory.user_id),
                )
                .join(SyncFeed, SyncFeed.installation_id == SyncInstallation.id)
                .join(
                    SyncRecord,
                    and_(
                        SyncRecord.feed_id == SyncFeed.id,
                        SyncRecord.doc_id == Memory.resource_doc_id,
                        SyncRecord.version == Memory.resource_version,
                        SyncRecord.payload.is_not(None),
                        stored.is_not_distinct_from(SyncRecord.payload),
                    ),
                )
                .where(
                    Memory.id == memory.id,
                    Memory.context_id == target.context_id,
                    Memory.deleted_at.is_(None),
                    Memory.version == 1,
                    Memory.source == "resource_ingest",
                )
                .limit(1)
            )
        ).first()
    return row is not None and tuple(row) == (
        memory.content,
        memory.context_id,
        memory.user_id,
        memory.resource_id,
        memory.resource_doc_id,
        memory.resource_version,
    )


async def fence_state_for_memories(
    db: AsyncSession, context_id: UUID, memory_ids: list[UUID]
) -> str | None:
    """Hold the shared fence of ``context_id`` and of each memory's current context.

    For a writer whose refusal depends on a row it reads next (a declared edge,
    #2907): every fence is held until the transaction ends, so the state
    returned cannot change before the write commits. Contexts are fenced in
    UUID order.

    Only memories in ``context_id``'s own workspace count: a memory id outside
    it is never looked up or fenced, so the answer (and a fence held by a
    transition) says nothing about another workspace's rows. The caller's own
    endpoint check answers for such an id.

    Returns:
        The first fenced state (``feed`` / ``stopping``) among those contexts,
        or ``None`` when each is unmanaged or ``manual``.
    """
    from models.auth import Context

    contexts = {context_id}
    workspace = select(Context.workspace_id).where(Context.id == context_id).scalar_subquery()
    with db.no_autoflush:
        current = await db.scalars(
            select(Memory.context_id).where(
                Memory.id.in_(memory_ids), Memory.workspace_id == workspace
            )
        )
        contexts.update(c for c in current.all() if isinstance(c, UUID))
    state: str | None = None
    for context in sorted(contexts, key=str):
        fence = await target_fence(db, context)
        if state is None and fence.state not in (None, "manual"):
            state = fence.state
    return state


async def assert_resource_mutable(db: AsyncSession, resource_pk: UUID) -> None:
    """Guard slug-addressed ingestion/schema mutations at their authoritative UUID."""
    from models.auth import Context

    with db.no_autoflush:
        contexts = (
            await db.scalars(
                select(Context.id)
                .join(
                    Resource,
                    (Resource.workspace_id == Context.workspace_id)
                    & (Resource.resource_id == Context.resource_id),
                )
                .where(Resource.id == resource_pk, Context.deleted_at.is_(None))
                .order_by(Context.id)
            )
        ).all()
    for context_id in contexts:
        if isinstance(context_id, UUID):
            await assert_context_mutable(db, context_id)


async def service_mode(
    db: AsyncSession, installation: SyncInstallation, *, publish: bool = False
) -> TargetFence:
    """Staging may collect a manual-mode preview; publication requires feed mode."""
    fence = await target_fence(db, installation.context_id)
    if fence.legacy is not None:
        from services.sync_management import enroll

        fence = await enroll(db, installation)
    if fence.mode is not None and (
        fence.mode.generation != installation.generation
        or fence.mode.mode == "stopping"
        or (publish and fence.mode.mode != "feed")
    ):
        raise ManagedDataLockedError(fence.mode.mode)
    return fence


async def event_applicable(db: AsyncSession, event: object, context_id: UUID) -> bool:
    """A target writer retains the shared fence through its SQL/vector transaction."""
    from models.resource import ResourceEvent

    assert isinstance(event, ResourceEvent)
    fence = await target_fence(db, context_id)
    mode = fence.mode
    if mode is None:
        # A rollout target is conservatively fenced before its first enrollment.
        if fence.legacy is not None:
            raise ConflictError("Enroll the retained synchronization target before indexing")
        return True
    metadata = event.event_metadata if isinstance(event.event_metadata, dict) else {}
    if "sync_run_id" not in metadata:
        return mode.mode == "manual" and event.id > mode.event_floor
    if mode.mode == "manual":
        return False
    expected = mode.drain_generation if mode.mode == "stopping" else mode.generation
    return metadata.get("installation_generation") == expected and (
        mode.mode != "stopping" or event.id <= mode.stop_event_id
    )
