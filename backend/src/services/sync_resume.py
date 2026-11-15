"""Finite service-authenticated candidates and exact, current human resume consent."""

import json
from contextlib import AsyncExitStack
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal
from models.auth import Context
from models.management_schemas import digest
from models.memory import REVISION_KIND_FORGOTTEN, Memory
from models.resource import IndexerState, ResourceEvent
from models.sync_feed import SyncFeed, SyncIndexOutbox, SyncRecord, SyncRun
from models.sync_feed_schemas import MAX_RECORDS, MAX_RUN_BYTES, SCHEMA_HASH, FeedRecord
from models.sync_installation import SyncInstallation
from models.sync_management import (
    SyncManagedTarget,
    SyncManualRetirement,
    SyncResumeCandidate,
    SyncResumePreview,
)
from services.integration_entitlements import paid_integration_write
from services.sync_credentials import CredentialAuthority
from services.sync_feed_authority import FeedFence, current_schema, exact_target, locked
from services.sync_feed_commit import document_id
from services.sync_feed_runs import clear_staging, editable, generations, run_for, staged_records
from services.sync_managed_guard import service_mode
from services.sync_management import (
    audit_mode,
    discard_pending_runs,
    enroll,
    management_boundary,
    mode_view,
    operation_receipt,
    save_operation,
)
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import ConflictError, NotFoundException


def memory_hash(memory: Memory) -> str:
    """Bind the complete authored representation, identity, ownership and tombstone."""
    from models.memory import VERSIONED_FIELDS

    value = {name: getattr(memory, name) for name in VERSIONED_FIELDS}
    value.update(
        id=str(memory.id),
        context_id=str(memory.context_id),
        workspace_id=str(memory.workspace_id),
        author=memory.user_id,
        source=memory.source,
        source_type=memory.source_type,
        delivery_mode=memory.delivery_mode,
        context=memory.context,
        scope=memory.scope,
        long_term=memory.long_term,
        confidence=memory.confidence,
        source_uri=memory.source_uri,
        client=memory.client,
        client_version=memory.client_version,
        created_at=to_utc_iso(memory.created_at),
        updated_at=to_utc_iso(memory.updated_at),
        promoted_at=to_utc_iso(memory.promoted_at),
        agent_id=str(memory.agent_id) if memory.agent_id else None,
        session_id=memory.session_id,
        version=memory.version,
        deleted_at=to_utc_iso(memory.deleted_at),
        deleted_by=memory.deleted_by,
    )
    return digest(value)


async def pin_candidate(
    db: AsyncSession, authority: CredentialAuthority, rid: UUID, fence: FeedFence
) -> dict:
    """Close one bounded upload and pin one candidate per feed, up to the aggregate cap."""
    target = (await service_mode(db, authority.installation)).mode
    if target is None or target.mode != "manual":
        raise ConflictError("Resume candidates require manual mode")
    row, feed = await run_for(db, authority, rid)
    old = await db.get(SyncResumeCandidate, rid)
    if old is not None:
        return {
            "candidate_id": str(old.id),
            "active": old.active,
            "expires_at": to_utc_iso(old.expires_at),
        }
    await editable(db, authority, row, feed, fence)
    if row.state != "prepared" or row.prepared_until is None or utcnow() >= row.prepared_until:
        raise ConflictError("Prepare a fresh resume candidate first")
    fence.deadlines.append(row.prepared_until)
    records = await staged_records(db, row)
    size = len(json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode())
    active = (
        await db.scalars(
            select(SyncResumeCandidate).where(
                SyncResumeCandidate.installation_id == authority.installation.id,
                SyncResumeCandidate.active,
            )
        )
    ).all()
    other = [item for item in active if item.feed_id != feed.id and utcnow() < item.expires_at]
    if (
        sum(item.record_count for item in other) + len(records) > MAX_RECORDS
        or sum(item.byte_count for item in other) + size > MAX_RUN_BYTES
    ):
        raise ConflictError("Resume snapshot exceeds installation limits")
    for item in active:
        if item.feed_id == feed.id or utcnow() >= item.expires_at:
            item.active, item.records = False, None
    await db.flush()
    now = utcnow()
    candidate = SyncResumeCandidate(
        id=rid,
        installation_id=authority.installation.id,
        feed_id=feed.id,
        epoch=target.epoch,
        generations=generations(authority),
        manifest=row.manifest,
        records=records,
        record_count=len(records),
        byte_count=size,
        snapshot_hash=row.manifest["snapshot_hash"],
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    db.add(candidate)
    row.state = "aborted"
    await clear_staging(db, row)
    await db.flush()
    return {
        "candidate_id": str(rid),
        "active": True,
        "expires_at": to_utc_iso(candidate.expires_at),
    }


async def manual_snapshot(
    db: AsyncSession, installation: SyncInstallation, actor: str
) -> tuple[list[Memory], int, str]:
    """Do not erase or expose a row this exact caller cannot normally read and write."""
    from services.permission_service import CallerId, MemoryAuthorId, PermissionService

    summaries = (
        await db.execute(
            select(Memory.id, func.pg_column_size(text("memories")))
            .where(Memory.context_id == installation.context_id)
            .limit(20001)
        )
    ).all()
    if len(summaries) > 20000 or sum(item[1] for item in summaries) > 2 * MAX_RUN_BYTES:
        raise ConflictError("Manual snapshot exceeds resume history limits")
    try:
        memories = list(
            (
                await db.scalars(
                    select(Memory)
                    .where(Memory.context_id == installation.context_id)
                    .order_by(Memory.id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
    except DBAPIError as exc:
        from utils.db_helpers import is_lock_not_available

        if is_lock_not_available(exc):
            raise ConflictError("Manual data is changing; retry the preview") from exc
        raise
    if sum(memory.deleted_at is None for memory in memories) > MAX_RECORDS:
        raise ConflictError("Manual snapshot exceeds resume limits")
    permission = PermissionService(db)
    checked = set()
    for memory in memories:
        identity = (
            memory.user_id,
            memory.workspace_id,
            memory.context_id,
            memory.type,
            memory.source_type,
        )
        if identity in checked:
            continue
        checked.add(identity)
        for access in ("read", "write"):
            if not await permission.can_access_memory(
                user_id=CallerId(actor),
                memory_user_id=MemoryAuthorId(memory.user_id),
                workspace_id=memory.workspace_id,
                context_id=memory.context_id,
                access=access,
                memory_type=memory.type,
                memory_source_type=memory.source_type,
                key_workspace_id=installation.workspace_id,
            ):
                raise NotFoundException("Resume snapshot")
    watermark = (
        await db.scalar(
            select(func.max(ResourceEvent.id)).where(
                ResourceEvent.resource_pk == installation.resource_pk
            )
        )
    ) or 0
    state = await db.scalar(
        select(IndexerState).where(
            IndexerState.resource_pk == installation.resource_pk,
            IndexerState.context_id == installation.context_id,
        )
    )
    if watermark and (
        state is None or state.last_offset < watermark or (state.metrics or {}).get("failed_events")
    ):
        raise ConflictError("Finish pending manual ingestion before previewing resume")
    return (
        memories,
        watermark,
        digest({"memories": [memory_hash(memory) for memory in memories], "watermark": watermark}),
    )


async def candidates(
    db: AsyncSession, installation: SyncInstallation, target: SyncManagedTarget, fence: FeedFence
) -> list[tuple[SyncFeed, SyncResumeCandidate]]:
    """Every configured feed must have a fresh strict snapshot at current authority."""
    from models.sync_credential import SyncCredentialClient
    from models.sync_installation import SyncService

    service = await locked(db, SyncService, SyncService.service_id == installation.service_id)
    client = await locked(
        db, SyncCredentialClient, SyncCredentialClient.client_id == installation.service_client_id
    )
    if service is None or client is None or not service.enabled or not client.enabled:
        raise ConflictError("Synchronization service is unavailable")
    schema = await current_schema(db, installation)
    if schema is None or digest(schema.field_definitions) != SCHEMA_HASH:
        raise ConflictError("Approved synchronization schema changed")
    feeds = (
        await db.scalars(
            select(SyncFeed)
            .where(SyncFeed.installation_id == installation.id)
            .order_by(SyncFeed.id)
        )
    ).all()
    if not feeds:
        raise ConflictError("Configure a synchronization feed first")
    result = []
    for feed in feeds:
        row = await db.scalar(
            select(SyncResumeCandidate).where(
                SyncResumeCandidate.feed_id == feed.id, SyncResumeCandidate.active
            )
        )
        if (
            row is None
            or row.records is None
            or row.epoch != target.epoch
            or utcnow() >= row.expires_at
            or row.generations != [service.generation, client.generation, installation.generation]
            or row.manifest["config_hash"] != feed.config_hash
            or row.manifest["config_version"] != feed.config_version
            or row.manifest["schema_version"] != schema.schema_version
        ):
            raise ConflictError("Collect fresh resume candidates for every feed")
        fence.deadlines.append(row.expires_at)
        result.append((feed, row))
    return result


def source_hash(rows: list[tuple[SyncFeed, SyncResumeCandidate]]) -> str:
    """Pin receipt identity, complete manifest, configuration and canonical revision."""
    return digest(
        [
            {
                "feed": str(feed.id),
                "revision": feed.revision,
                "candidate": str(row.id),
                "manifest": row.manifest,
                "epoch": row.epoch,
                "generations": row.generations,
            }
            for feed, row in rows
        ]
    )


async def resume_preview(db: AsyncSession, wid: UUID, iid: UUID, principal: SyncPrincipal) -> dict:
    """Persist a fifteen-minute exact diff; replacement invalidates previous consent."""
    async with management_boundary(db, wid, iid, principal) as (installation, workspace, fence):
        target = (await enroll(db, installation)).mode
        if target is None or target.mode != "manual" or installation.state != "active":
            raise ConflictError("An active manual installation is required")
        await exact_target(db, installation)
        rows = await candidates(db, installation, target, fence)
        memories, _watermark, manual_hash = await manual_snapshot(
            db, installation, principal.user_id
        )
        # Hashes and normal identifiers, never arbitrary manual text, form the wire diff.
        diff = [
            {
                "op": "retire",
                "memory_id": str(memory.id),
                "before_hash": memory_hash(memory),
                "summary": memory.summary,
            }
            for memory in memories
            if memory.deleted_at is None
        ]
        diff += [
            {
                "op": "publish",
                "feed_id": str(feed.id),
                "external_id": record["external_id"],
                "fields": FeedRecord.model_validate(record).public_payload(feed.kind),
            }
            for feed, candidate in rows
            for record in candidate.records or []
        ]
        previous = await db.get(SyncResumePreview, iid)
        if previous is not None:
            await db.delete(previous)
            await db.flush()
        sid = source_hash(rows)
        preview_id = uuid4()
        value = digest(
            {
                "id": str(preview_id),
                "manual": manual_hash,
                "source": sid,
                "epoch": target.epoch,
                "generation": installation.generation,
                "ownership_epoch": workspace.ownership_epoch,
            }
        )
        now = utcnow()
        preview = SyncResumePreview(
            installation_id=iid,
            id=preview_id,
            approved_by=principal.user_id,
            ownership_epoch=workspace.ownership_epoch,
            epoch=target.epoch,
            generation=installation.generation,
            manual_hash=manual_hash,
            source_hash=sid,
            diff_hash=value,
            totals={
                "retire": sum(item["op"] == "retire" for item in diff),
                "publish": sum(item["op"] == "publish" for item in diff),
            },
            diff=diff,
            created_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        db.add(preview)
        fence.deadlines.append(preview.expires_at)
        await db.flush()
        await db.commit()
        return preview_view(preview)


def preview_view(row: SyncResumePreview) -> dict:
    """Finite confirmation identity, with separately paginated diff."""
    return {
        "preview_id": str(row.id),
        "diff_hash": row.diff_hash,
        "totals": row.totals,
        "expires_at": to_utc_iso(row.expires_at),
    }


async def resume_diff(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    preview_id: UUID,
    offset: int,
    limit: int,
) -> dict:
    """Each page rechecks the same human's current normal permissions and authority."""
    async with management_boundary(db, wid, iid, principal) as (installation, workspace, fence):
        target = (await enroll(db, installation)).mode
        preview = await db.get(SyncResumePreview, iid)
        if (
            preview is None
            or preview.id != preview_id
            or preview.approved_by != principal.user_id
            or utcnow() >= preview.expires_at
        ):
            raise ConflictError("Obtain a fresh resume preview")
        assert target is not None
        fence.deadlines.append(preview.expires_at)
        rows = await candidates(db, installation, target, fence)
        _memories, _watermark, manual_hash = await manual_snapshot(
            db, installation, principal.user_id
        )
        if (
            target.mode != "manual"
            or target.epoch != preview.epoch
            or workspace.ownership_epoch != preview.ownership_epoch
            or installation.generation != preview.generation
            or manual_hash != preview.manual_hash
            or source_hash(rows) != preview.source_hash
        ):
            raise ConflictError("Resume diff changed; obtain a fresh preview")
        result = {
            **preview_view(preview),
            "items": preview.diff[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(preview.diff) else None,
        }
        await db.flush()
        await db.commit()
        return result


async def retire_manual(
    db: AsyncSession, installation: SyncInstallation, memories: list[Memory], actor: str
) -> None:
    """Keep authored history; record exact tombstone proofs and durable point cleanup."""
    from repositories.neural_edge import NeuralEdgeRepository
    from services.context_routing import resolve_collection_name
    from services.deferred_point_deletes import queue_point_deletes
    from services.memory_versions import record_revision
    from services.operation_history_writer import record_operation

    context = await db.get(Context, installation.context_id)
    assert context is not None
    collection = await resolve_collection_name(db, context.id)
    now = utcnow()
    entries = []
    proofs = {
        row.memory_id: row
        for row in (
            await db.scalars(
                select(SyncManualRetirement).where(
                    SyncManualRetirement.installation_id == installation.id,
                    SyncManualRetirement.memory_id.in_([memory.id for memory in memories]),
                )
            )
        ).all()
    }
    for memory in memories:
        if memory.deleted_at is None:
            memory.deleted_at, memory.deleted_by = now, actor
            memory.agent_id = memory.session_id = None
            record_revision(db, memory, kind=REVISION_KIND_FORGOTTEN, actor_user_id=actor, at=now)
            entries.append((memory.id, memory.point_id, collection))
            await NeuralEdgeRepository(db).delete_node_edges(
                user_id=memory.user_id,
                node_id=memory.id,
                workspace_id=str(installation.workspace_id),
                context_id=str(context.id),
            )
        assert memory.deleted_at is not None
        proof = proofs.get(memory.id)
        if proof is None:
            proof = SyncManualRetirement(
                installation_id=installation.id,
                memory_id=memory.id,
                generation=installation.generation,
                memory_version=memory.version,
                content_hash=memory_hash(memory),
                deleted_at=memory.deleted_at,
                approved_by=actor,
            )
            db.add(proof)
        else:
            proof.generation, proof.memory_version = installation.generation, memory.version
            proof.content_hash, proof.deleted_at, proof.approved_by = (
                memory_hash(memory),
                memory.deleted_at,
                actor,
            )
    await queue_point_deletes(db, entries)
    if entries:
        record_operation(
            db,
            operation="memory.deleted",
            actor_user_id=actor,
            surface="api",
            context=context,
            count=len(entries),
        )


async def publish_resumed_feed(
    db: AsyncSession, installation: SyncInstallation, feed: SyncFeed, candidate: SyncResumeCandidate
) -> None:
    """Publish human-approved strict snapshots; no service or owner impersonation."""
    incoming = {
        item["external_id"]: FeedRecord.model_validate(item).public_payload(feed.kind)
        for item in candidate.records or []
    }
    previous = {
        item.external_id: item
        for item in (
            await db.scalars(
                select(SyncRecord)
                .where(
                    SyncRecord.feed_id == feed.id,
                    or_(
                        SyncRecord.payload.is_not(None), SyncRecord.external_id.in_(list(incoming))
                    ),
                )
                .limit(20001)
            )
        ).all()
    }
    ids = sorted(set(incoming) | set(previous))
    if len(ids) > 20000:
        raise ConflictError("Resume canonical history limit exceeded")
    versions = dict(
        (
            await db.execute(
                select(ResourceEvent.doc_id, func.max(ResourceEvent.version))
                .where(
                    ResourceEvent.resource_pk == installation.resource_pk,
                    ResourceEvent.doc_id.in_(
                        [document_id(installation.id, feed.id, eid) for eid in ids]
                    ),
                )
                .group_by(ResourceEvent.doc_id)
            )
        ).all()
    )
    events = []
    diff = []
    for eid in ids:
        old = previous.get(eid)
        if eid not in incoming and (old is None or old.payload is None):
            continue
        doc = document_id(installation.id, feed.id, eid)
        version = max(versions.get(doc) or 0, old.version if old else 0) + 1
        op = "upsert" if eid in incoming else "delete"
        diff.append(
            {
                "external_id": eid,
                "op": op,
                "before_hash": digest(old.payload if old else None),
                "after_hash": digest(incoming.get(eid)),
            }
        )
        event = ResourceEvent(
            resource_pk=installation.resource_pk,
            resource_id=installation.resource_id,
            op=op,
            doc_id=doc,
            version=version,
            payload=incoming.get(eid),
            idempotency_key=f"sync:{candidate.id}:{doc}:{version}",
            event_metadata={
                "sync_run_id": str(candidate.id),
                "sync_feed_id": str(feed.id),
                "installation_generation": installation.generation,
            },
        )
        db.add(event)
        events.append((eid, event))
    await db.flush()
    for eid, event in events:
        old = previous.get(eid)
        if old is None:
            db.add(
                SyncRecord(
                    feed_id=feed.id,
                    resource_pk=installation.resource_pk,
                    external_id=eid,
                    doc_id=event.doc_id,
                    version=event.version,
                    payload=incoming.get(eid),
                    last_event_id=event.id,
                )
            )
        else:
            old.version, old.payload, old.last_event_id = event.version, incoming.get(eid), event.id
    run = await db.get(SyncRun, candidate.id)
    assert run is not None
    run.base_revision, run.diff, run.state, run.committed_at = (
        feed.revision,
        diff,
        "committed",
        utcnow(),
    )
    run.generations = [candidate.generations[0], candidate.generations[1], installation.generation]
    run.commit_hash = digest({"resume": str(candidate.id), "generation": installation.generation})
    feed.revision += 1
    if events:
        db.add(
            SyncIndexOutbox(
                run_id=run.id,
                resource_pk=installation.resource_pk,
                context_id=installation.context_id,
                last_event_id=events[-1][1].id,
                event_ids=[event.id for _eid, event in events],
            )
        )
    candidate.active, candidate.records = False, None
    await db.flush()


async def confirm_resume(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    operation_id: UUID,
    preview_id: UUID,
    diff_hash: str,
) -> dict:
    """One paid, human-authorized SQL commit publishes all feeds and reclaims the target."""
    initial = await db.scalar(
        select(SyncInstallation).where(
            SyncInstallation.id == iid, SyncInstallation.workspace_id == wid
        )
    )
    if initial is None:
        raise NotFoundException("Synchronization target")
    request = {"preview_id": str(preview_id), "diff_hash": diff_hash}
    async with AsyncExitStack() as boundaries:
        async with paid_integration_write(db, wid, initial.service_id):
            installation, workspace, fence = await boundaries.enter_async_context(
                management_boundary(db, wid, iid, principal)
            )
            receipt = await operation_receipt(
                db, iid, operation_id, "resume", request, principal.user_id
            )
            if receipt is not None:
                return receipt.result
            target = (await enroll(db, installation)).mode
            preview = await db.get(SyncResumePreview, iid)
            if (
                target is None
                or target.mode != "manual"
                or installation.state != "active"
                or installation.revoked_at is not None
            ):
                raise ConflictError("An active manual installation is required")
            await exact_target(db, installation)
            rows = await candidates(db, installation, target, fence)
            memories, watermark, manual_hash = await manual_snapshot(
                db, installation, principal.user_id
            )
            if (
                preview is None
                or preview.id != preview_id
                or preview.diff_hash != diff_hash
                or preview.approved_by != principal.user_id
                or preview.ownership_epoch != workspace.ownership_epoch
                or target.epoch != preview.epoch
                or installation.generation != preview.generation
                or utcnow() >= preview.expires_at
                or manual_hash != preview.manual_hash
                or source_hash(rows) != preview.source_hash
            ):
                raise ConflictError("Resume diff changed; obtain a fresh preview")
            fence.deadlines.append(preview.expires_at)
            from services.sync_feed_authority import worker_target

            await worker_target(db, installation, content_write=True)
            installation.generation += 1
            await discard_pending_runs(db, installation.id)
            await retire_manual(db, installation, memories, principal.user_id)
            for feed, candidate in rows:
                await publish_resumed_feed(db, installation, feed, candidate)
            target.mode, target.generation, target.event_floor = (
                "feed",
                installation.generation,
                watermark,
            )
            target.epoch += 1
            target.stop_reason, target.drain_generation, target.updated_at = None, None, utcnow()
            context = await db.get(Context, installation.context_id)
            assert context is not None
            target.manual_sleep_mode, context.sleep_mode = context.sleep_mode, "skip"
            audit_mode(db, installation, "resumed", principal.user_id)
            result = mode_view(target)
            save_operation(db, iid, operation_id, "resume", request, principal.user_id, result)
            await db.delete(preview)
            return result


async def reconnect(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    operation_id: UUID,
    authorization_id: UUID,
) -> dict:
    """Fresh owner consent reuses retained UUIDs, without silently leaving manual mode."""
    from services.sync_authority import audit_sync, registered_service, sync_authority
    from services.sync_installations import _approval, _validate_pending

    initial = await db.scalar(
        select(SyncInstallation).where(
            SyncInstallation.id == iid, SyncInstallation.workspace_id == wid
        )
    )
    if initial is None:
        raise NotFoundException("Synchronization target")
    request = {"authorization_id": str(authorization_id)}
    async with AsyncExitStack() as boundaries:
        async with paid_integration_write(db, wid, initial.service_id):
            authority = await boundaries.enter_async_context(
                sync_authority(db, wid, principal, paid_guarded=True)
            )
            installation, _workspace, fence = await boundaries.enter_async_context(
                management_boundary(db, wid, iid, principal)
            )
            receipt = await operation_receipt(
                db, iid, operation_id, "reconnect", request, principal.user_id
            )
            if receipt is not None:
                return receipt.result
            target = (await enroll(db, installation)).mode
            if target is None or target.mode != "manual" or installation.state != "revoked":
                raise ConflictError("Finish unlinking into manual mode before reconnecting")
            service = await registered_service(db, browser_client_id=principal.client_id)
            approval = await _approval(db, wid, authorization_id)
            _validate_pending(approval, authority, service)
            if (approval.service_id, approval.application_id, approval.service_client_id) != (
                installation.service_id,
                installation.application_id,
                installation.service_client_id,
            ):
                raise ConflictError("Owner consent does not match the retained installation")
            await exact_target(db, installation)
            fence.deadlines.append(approval.expires_at)
            approval.used_at = utcnow()
            installation.authorization_id = approval.id
            installation.state, installation.revoked_at = "active", None
            installation.generation += 1
            target.generation, target.epoch = installation.generation, target.epoch + 1
            audit_sync(db, authority, "sync.installation.reconnected", iid)
            result = mode_view(target)
            save_operation(db, iid, operation_id, "reconnect", request, principal.user_id, result)
            return result
