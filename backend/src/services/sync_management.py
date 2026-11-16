"""Durable ownership barrier; stopping never grants ordinary editing authority."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, case, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal, load_sync_identity
from auth.workspace_roles import ContextRole
from config.settings import get_settings
from models.auth import Context, IdentityLink, User, Workspace
from models.management_schemas import digest
from models.memory import Memory
from models.resource import IndexerState, ResourceEvent
from models.sync_feed import SyncChunk, SyncFeed, SyncRecord, SyncRun
from models.sync_installation import SyncInstallation, SyncService
from models.sync_management import (
    SyncManagedTarget,
    SyncModeOperation,
    SyncResumeCandidate,
    SyncResumePreview,
)
from models.sync_mode_access_schemas import (
    SyncModeAccess,
    SyncModePermissions,
    SyncModeStatus,
    SyncModeTarget,
)
from services.capacity_lock import CapacityPolicy
from services.permission_service import PermissionService
from services.sync_feed_authority import FeedFence, feed_fence, locked
from services.sync_feed_index import index_status
from services.sync_managed_guard import TargetFence, target_fence
from services.workspace_locks import lock_workspace_for_update
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException


@dataclass
class MaintenanceScan:
    """A process continues to the next UUID page even after a failed drain."""

    after: UUID | None = None


_maintenance_scan = MaintenanceScan()


async def discard_pending_runs(db: AsyncSession, iid: UUID) -> None:
    """A generation transition releases old staging slots and payloads atomically."""
    pending = (
        select(SyncRun.id)
        .join(SyncFeed)
        .where(SyncFeed.installation_id == iid, SyncRun.state.in_(["uploading", "prepared"]))
    )
    await db.execute(update(SyncChunk).where(SyncChunk.run_id.in_(pending)).values(records=None))
    await db.execute(
        update(SyncRun)
        .where(SyncRun.id.in_(pending))
        .values(state="aborted", approval_id=None, approved_by=None, approval_epoch=None)
    )


async def canonical_index_complete(
    db: AsyncSession, installation: SyncInstallation, *, feed_id: UUID | None = None
) -> bool:
    """Current rows/deletions prove completeness even when the latest run was a no-op."""
    context = await db.get(Context, installation.context_id)
    if context is None:
        return False
    content = case(
        (func.pg_input_is_valid(Memory.content, "jsonb"), cast(Memory.content, JSONB)), else_=None
    )
    live = and_(
        Memory.context_id == installation.context_id,
        Memory.resource_id == installation.resource_id,
        Memory.resource_doc_id == SyncRecord.doc_id,
        Memory.deleted_at.is_(None),
    )
    invalid = await db.scalar(
        select(SyncRecord.id)
        .join(SyncFeed)
        .outerjoin(Memory, live)
        .where(
            SyncFeed.installation_id == installation.id,
            SyncRecord.payload.is_not(None),
            *([SyncRecord.feed_id == feed_id] if feed_id is not None else []),
        )
        .group_by(SyncRecord.id)
        .having(
            or_(
                func.count(Memory.id) != 1,
                func.bool_or(
                    or_(
                        Memory.version != 1,
                        Memory.resource_version.is_distinct_from(SyncRecord.version),
                        Memory.embedding_status != "success",
                        Memory.source != "resource_ingest",
                        Memory.user_id != str(context.created_by),
                        content.is_distinct_from(SyncRecord.payload),
                    )
                ),
            )
        )
        .limit(1)
    )
    deleted = await db.scalar(
        select(SyncRecord.id)
        .join(SyncFeed)
        .join(Memory, live)
        .where(
            SyncFeed.installation_id == installation.id,
            SyncRecord.payload.is_(None),
            *([SyncRecord.feed_id == feed_id] if feed_id is not None else []),
        )
        .limit(1)
    )
    return invalid is None and deleted is None


def management_enabled() -> None:
    """The new controls are opt-in; persisted locks are enforced regardless."""
    if not get_settings().sync_managed_enabled:
        raise NotFoundException("Synchronization management")


async def enroll(db: AsyncSession, installation: SyncInstallation) -> TargetFence:
    """Take an exclusive absent-row fence before enrolling the retained fixed target."""
    fence = await target_fence(db, installation.context_id, exclusive=True)
    if fence.mode is None:
        context = await locked(db, Context, Context.id == installation.context_id, write=True)
        if context is None or context.deleted_at is not None:
            raise NotFoundException("Synchronization target")
        from services.sync_feed_provenance import enrolled_event

        state = await db.scalar(
            select(IndexerState).where(
                IndexerState.resource_pk == installation.resource_pk,
                IndexerState.context_id == installation.context_id,
            )
        )
        ordinary_pending = await db.scalar(
            select(ResourceEvent.id)
            .where(
                ResourceEvent.resource_pk == installation.resource_pk,
                ResourceEvent.id > (state.last_offset if state is not None else 0),
                ~enrolled_event(),
            )
            .limit(1)
        )
        if ordinary_pending is not None:
            raise ConflictError(
                "Drain ordinary ingestion before enrolling synchronization ownership"
            )
        row = SyncManagedTarget(
            installation_id=installation.id,
            resource_pk=installation.resource_pk,
            context_id=installation.context_id,
            mode="feed",
            epoch=1,
            generation=installation.generation,
            event_floor=0,
            stop_event_id=0,
            manual_sleep_mode=context.sleep_mode,
        )
        db.add(row)
        context.sleep_mode = "skip"
        fence.mode, fence.legacy = row, None
        if installation.state == "revoked" or installation.revoked_at is not None:
            # Pre-barrier unlink advanced the installation generation after
            # accepting these receipts. Drain that real historical generation;
            # never acknowledge its still-pending events as obsolete.
            prior = (
                await db.scalars(
                    select(SyncRun.generations[2].as_integer())
                    .join(SyncFeed)
                    .where(
                        SyncFeed.installation_id == installation.id, SyncRun.state == "committed"
                    )
                    .distinct()
                    .limit(2)
                )
            ).all()
            if len(prior) > 1:
                raise ConflictError(
                    "Legacy synchronization generations require operator reconciliation"
                )
            row.mode, row.stop_reason = "stopping", "legacy_unlinked"
            row.drain_generation = prior[0] if prior else installation.generation
            row.stop_event_id = (
                await db.scalar(
                    select(func.max(ResourceEvent.id)).where(
                        ResourceEvent.resource_pk == row.resource_pk
                    )
                )
            ) or 0
        await db.flush()
    if (fence.mode.installation_id, fence.mode.resource_pk) != (
        installation.id,
        installation.resource_pk,
    ):
        raise ConflictError("Synchronization ownership changed")
    return fence


def mode_view(row: SyncManagedTarget) -> dict:
    """No content, private source configuration or credentials leave this status."""
    return {
        "mode": row.mode,
        "epoch": row.epoch,
        "generation": row.generation,
        "stop_event_id": row.stop_event_id,
        "reason": row.stop_reason,
    }


def audit_mode(
    db: AsyncSession,
    installation: SyncInstallation,
    action: str,
    actor: str | None,
    *,
    target: SyncManagedTarget,
) -> None:
    """Attribute to the actual human or system, never to an impersonated owner."""
    from services.sync_audit import add_audit, current_actor

    add_audit(
        db,
        "sync.management." + action,
        current_actor(actor),
        installation=installation,
        mode=target.mode,
        management_epoch=target.epoch,
        stop_watermark=target.stop_event_id,
        drain_generation=target.drain_generation,
    )


async def start_stopping(
    db: AsyncSession, installation: SyncInstallation, reason: str, *, actor: str | None = None
) -> SyncManagedTarget:
    """Workspace lock is held by callers; all unfinished uploads lose authority atomically."""
    fence = await enroll(db, installation)
    row = fence.mode
    assert row is not None
    if row.mode != "feed":
        return row
    row.drain_generation = row.generation
    installation.generation += 1
    row.generation = installation.generation
    row.epoch += 1
    row.mode, row.stop_reason, row.updated_at = "stopping", reason, utcnow()
    row.stop_event_id = (
        await db.scalar(
            select(func.max(ResourceEvent.id)).where(ResourceEvent.resource_pk == row.resource_pk)
        )
    ) or 0
    await discard_pending_runs(db, installation.id)
    for candidate in (
        await db.scalars(
            select(SyncResumeCandidate).where(
                SyncResumeCandidate.installation_id == installation.id, SyncResumeCandidate.active
            )
        )
    ).all():
        candidate.active, candidate.records = False, None
    preview = await db.get(SyncResumePreview, installation.id)
    if preview is not None:
        await db.delete(preview)
    audit_mode(db, installation, "stopping", actor, target=row)
    await db.flush()
    return row


async def finish_stopping(
    db: AsyncSession, installation: SyncInstallation, *, actor: str | None = None
) -> SyncManagedTarget:
    """Exclusive target fence proves no pre-stop SQL/vector writer remains in flight."""
    fence = await enroll(db, installation)
    row = fence.mode
    assert row is not None
    if row.mode != "stopping":
        return row
    state = await db.scalar(
        select(IndexerState).where(
            IndexerState.resource_pk == row.resource_pk, IndexerState.context_id == row.context_id
        )
    )
    if row.stop_event_id and (state is None or state.last_offset < row.stop_event_id):
        raise ConflictError("Synchronization is still draining")
    if state is not None and any(
        item.get("event_id", 0) <= row.stop_event_id
        for item in (state.metrics or {}).get("failed_events", [])
    ):
        raise ConflictError("Repair failed synchronization events before unlocking")
    feeds = (
        await db.scalars(select(SyncFeed).where(SyncFeed.installation_id == installation.id))
    ).all()
    for feed in feeds:
        run = await db.scalar(
            select(SyncRun)
            .where(
                SyncRun.feed_id == feed.id,
                SyncRun.state == "committed",
                SyncRun.base_revision == feed.revision - 1,
            )
            .order_by(SyncRun.base_revision.desc())
            .limit(1)
        )
        if run is not None and await index_status(db, run, feed) not in {"complete", "unchanged"}:
            raise ConflictError("Canonical synchronization data has not finished indexing")
    if not await canonical_index_complete(db, installation):
        raise ConflictError("Canonical synchronization data has not finished indexing")
    context = await locked(db, Context, Context.id == row.context_id, write=True)
    if context is None or context.deleted_at is not None:
        raise NotFoundException("Synchronization target")
    row.mode, row.event_floor, row.updated_at = "manual", row.stop_event_id, utcnow()
    row.epoch += 1
    context.sleep_mode = row.manual_sleep_mode
    audit_mode(db, installation, "manual", actor, target=row)
    await db.flush()
    return row


@asynccontextmanager
async def management_boundary(
    db: AsyncSession, wid: UUID, iid: UUID, principal: SyncPrincipal, *, write: bool = True
) -> AsyncIterator[tuple[SyncInstallation, Workspace, FeedFence]]:
    """Stopping remains available after revocation/expiry and under a capacity hold."""
    management_enabled()
    async with feed_fence(db) as fence:
        try:
            await locked(db, User, User.user_id == principal.user_id)
            workspace = await lock_workspace_for_update(db, wid, nowait=True)
            live = await load_sync_identity(
                db,
                principal.token_hash,
                "integrations:manage" if write else "workspaces:read",
                lock=True,
            )
            row = await locked(
                db,
                SyncInstallation,
                SyncInstallation.id == iid,
                SyncInstallation.workspace_id == wid,
                write=True,
            )
            if row is None or (live.user_id, live.client_id) != (
                principal.user_id,
                principal.client_id,
            ):
                raise NotFoundException("Synchronization target")
            service = await locked(db, SyncService, SyncService.service_id == row.service_id)
            if service is None or service.browser_client_id != live.client_id:
                raise NotFoundException("Synchronization target")
            await PermissionService(db).check_context_access(
                live.user_id,
                row.context_id,
                required_role=ContextRole.EDITOR if write else ContextRole.VIEWER,
                capacity=CapacityPolicy.ALLOWED,
                key_workspace_id=wid,
            )
            # Refresh/grant locks prevent a role/reach revocation racing the final commit.
            from services.sync_feed_authority import lock_membership

            await lock_membership(
                db, live.user_id, row, write=write, capacity=CapacityPolicy.ALLOWED
            )
            fence.deadlines.append(live.expires_at)
            yield row, workspace, fence
        except BaseException:
            await db.rollback()
            raise


async def read_mode_access(
    db: AsyncSession, wid: UUID, iid: UUID, principal: SyncPrincipal
) -> SyncModeAccess:
    """Observe current lifecycle rights without enabling canonical data access."""
    async with management_boundary(db, wid, iid, principal, write=False) as (
        installation,
        _workspace,
        _fence,
    ):
        from services.sync_provisioning import installation_view

        current = await target_fence(db, installation.context_id)
        if current.mode is None:
            # A row moved away from this retained Context is drift, not an
            # unenrolled target whose conservative default may be projected.
            if await locked(db, SyncManagedTarget, SyncManagedTarget.installation_id == iid):
                raise ConflictError("Synchronization ownership changed")
        elif (
            current.mode.installation_id,
            current.mode.resource_pk,
            current.mode.context_id,
            current.mode.generation,
        ) != (iid, installation.resource_pk, installation.context_id, installation.generation):
            raise ConflictError("Synchronization ownership changed")
        # A capability snapshot must not outlive the Context privacy/linked-owner
        # evidence used to authorize it. Refresh and retain those metadata locks
        # through the final RBAC check and commit, without requiring publication.
        context = await locked(db, Context, Context.id == installation.context_id)
        if context is None or context.deleted_at is not None:
            raise NotFoundException("Synchronization target")
        if context.is_private and context.created_by != principal.user_id:
            if context.created_by is None:
                raise NotFoundException("Synchronization target")
            groups = set()
            for user_id in sorted({principal.user_id, context.created_by}):
                link = await locked(db, IdentityLink, IdentityLink.user_id == user_id)
                if link is None:
                    raise NotFoundException("Synchronization target")
                groups.add(link.group_id)
            if len(groups) != 1:
                raise NotFoundException("Synchronization target")
        _context, role = await PermissionService(db).check_context_access(
            principal.user_id,
            installation.context_id,
            required_role=ContextRole.VIEWER,
            capacity=CapacityPolicy.ALLOWED,
            key_workspace_id=wid,
        )
        allowed = role in (ContextRole.EDITOR, ContextRole.OWNER)
        status = (
            mode_view(current.mode)
            if current.mode is not None
            else {
                "mode": "feed",
                "epoch": 1,
                "generation": installation.generation,
                "stop_event_id": 0,
                "reason": None,
            }
        )
        target = await installation_view(db, installation)
        result = SyncModeAccess(
            contract_version="sync-mode-access-v1",
            subject=principal.user_id,
            target=SyncModeTarget.model_validate(
                target.model_dump(
                    include={
                        "workspace_id",
                        "installation_id",
                        "resource_pk",
                        "context_id",
                        "endpoint_id",
                        "generation",
                        "target_available",
                    }
                )
            ),
            management=SyncModeStatus.model_validate(status),
            permissions=SyncModePermissions(read=True, stop=allowed, finalize=allowed),
        )
        await db.commit()
        return result


async def operation_receipt(
    db: AsyncSession, iid: UUID, operation_id: UUID, action: str, request: dict, actor: str
) -> SyncModeOperation | None:
    """Immutable retry identity is also bound to the real requesting principal."""
    row = await db.get(SyncModeOperation, (iid, operation_id))
    if row is not None and (
        row.action != action or row.request_hash != digest(request) or row.actor_user_id != actor
    ):
        raise ConflictError("Management operation identity was reused")
    return row


def save_operation(
    db: AsyncSession,
    iid: UUID,
    operation_id: UUID,
    action: str,
    request: dict,
    actor: str,
    result: dict,
) -> None:
    """Commit the result and transition in the same transaction."""
    db.add(
        SyncModeOperation(
            installation_id=iid,
            operation_id=operation_id,
            action=action,
            request_hash=digest(request),
            actor_user_id=actor,
            result=result,
        )
    )


async def control_mode(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    operation_id: UUID | None = None,
    *,
    finalize: bool = False,
    expected: dict | None = None,
) -> dict:
    """Read metadata or execute one idempotent stop/finalize command."""
    async with management_boundary(db, wid, iid, principal, write=operation_id is not None) as (
        installation,
        _workspace,
        _fence,
    ):
        action = "finalize" if finalize else "stop"
        request = {"expected": expected} if expected is not None else {}
        receipt = (
            await operation_receipt(db, iid, operation_id, action, request, principal.user_id)
            if operation_id
            else None
        )
        if receipt is not None:
            result = receipt.result
        else:
            if operation_id is None:
                current = await target_fence(db, installation.context_id)
                if current.mode is None:
                    return {
                        "mode": "feed",
                        "epoch": 1,
                        "generation": installation.generation,
                        "stop_event_id": 0,
                        "reason": None,
                    }
                target = current.mode
            else:
                if expected is not None:
                    # Check before enrollment or transition side effects. The workspace
                    # and exclusive target fences also protect the following mutation.
                    current = await target_fence(db, installation.context_id, exclusive=True)
                    observed = (
                        mode_view(current.mode)
                        if current.mode is not None
                        else {
                            "mode": "feed",
                            "epoch": 1,
                            "generation": installation.generation,
                            "stop_event_id": 0,
                        }
                    )
                    snapshot = {
                        key: observed[key]
                        for key in ("mode", "epoch", "generation", "stop_event_id")
                    }
                    if digest(expected) != digest(snapshot):
                        raise ConflictError("Confirmed management state changed")
                target = (
                    await finish_stopping(db, installation, actor=principal.user_id)
                    if finalize
                    else await start_stopping(
                        db, installation, "manual_request", actor=principal.user_id
                    )
                )
            assert target is not None
            result = mode_view(target)
            if operation_id is not None:
                save_operation(db, iid, operation_id, action, request, principal.user_id, result)
        await db.flush()
        await db.commit()
        return result


async def stop_ineligible_workspace(db: AsyncSession, wid: UUID) -> None:
    """Billing/plan mutations already own Workspace; eligibility loss uses the same barrier."""
    from services.integration_entitlements import inspect_paid_integration

    installation = await db.scalar(
        select(SyncInstallation).where(SyncInstallation.workspace_id == wid)
    )
    if installation is None:
        return
    fence = await target_fence(db, installation.context_id)
    if (
        fence.state is not None
        and not (await inspect_paid_integration(db, wid, installation.service_id)).allowed
    ):
        await start_stopping(db, installation, "entitlement_ended")


async def maintain_sync_management(db: AsyncSession) -> None:
    """Bounded periodic recovery catches clock expiry and crashes while retaining failed locks."""
    from models.sync_management import SyncResumeCandidate, SyncResumePreview

    query = (
        select(SyncManagedTarget.installation_id)
        .order_by(SyncManagedTarget.installation_id)
        .limit(100)
    )
    if _maintenance_scan.after is not None:
        query = query.where(SyncManagedTarget.installation_id > _maintenance_scan.after)
    rows = (await db.scalars(query)).all()
    if not rows and _maintenance_scan.after is not None:
        _maintenance_scan.after = None
        rows = (
            await db.scalars(
                select(SyncManagedTarget.installation_id)
                .order_by(SyncManagedTarget.installation_id)
                .limit(100)
            )
        ).all()
    if rows:
        # Progress even when every selected target is busy or awaits operator repair.
        _maintenance_scan.after = rows[-1]
    await db.rollback()
    for iid in rows:
        try:
            installation = await db.get(SyncInstallation, iid)
            if installation is None:
                continue
            await lock_workspace_for_update(db, installation.workspace_id, nowait=True)
            await stop_ineligible_workspace(db, installation.workspace_id)
            target = await db.get(SyncManagedTarget, iid)
            if target is not None and target.mode == "stopping":
                await finish_stopping(db, installation)
            if target is not None:
                target.updated_at = utcnow()
            for candidate in (
                await db.scalars(
                    select(SyncResumeCandidate).where(
                        SyncResumeCandidate.installation_id == iid,
                        SyncResumeCandidate.expires_at <= utcnow(),
                        SyncResumeCandidate.records.is_not(None),
                    )
                )
            ).all():
                candidate.active, candidate.records = False, None
            preview = await db.get(SyncResumePreview, iid)
            if preview is not None and utcnow() >= preview.expires_at:
                await db.delete(preview)
            await db.flush()
            await db.commit()
        except ConflictError:
            # Keep stopping durable even when finalization cannot yet succeed.
            await db.rollback()
            try:
                installation = await db.get(SyncInstallation, iid)
                if installation is not None:
                    await lock_workspace_for_update(db, installation.workspace_id, nowait=True)
                    await stop_ineligible_workspace(db, installation.workspace_id)
                    target = await db.get(SyncManagedTarget, iid)
                    if target is not None:
                        target.updated_at = utcnow()
                    await db.flush()
                    await db.commit()
            except ConflictError:
                await db.rollback()
        finally:
            await db.rollback()
