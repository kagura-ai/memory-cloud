"""Transaction fences shared by every ordinary writer and feed mode transition."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from config.settings import get_settings
from models.memory import Memory
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


async def assert_vector_memory_mutable(db: AsyncSession, memory_id: UUID) -> None:
    """Background vector writers resolve the authoritative current context first."""
    context_id = await db.scalar(select(Memory.context_id).where(Memory.id == memory_id))
    if isinstance(context_id, UUID):
        await assert_context_mutable(db, context_id)


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
