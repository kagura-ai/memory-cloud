"""Current-member operational snapshots; never a second authority or usage counter."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from auth.sync_identity import SyncPrincipal, load_sync_identity
from config.settings import get_settings
from models.auth import AuditLog, User, Workspace
from models.management_schemas import digest
from models.resource import Resource
from models.sync_activity import SyncManualRequest
from models.sync_feed import SyncChunk, SyncFeed, SyncRecord, SyncRun
from models.sync_installation import SyncInstallation, SyncService
from models.sync_management import SyncManagedTarget
from services.capacity_lock import CapacityPolicy
from services.public_usage_budget import read_public_usage_budget
from services.sync_audit import ACTIONS, RETENTION_DAYS, SCHEMA, bind_actor, event_view, human_actor
from services.sync_feed_authority import (
    FeedFence,
    current_schema,
    feed_fence,
    lock_membership,
    locked,
)
from services.sync_feed_index import index_status
from services.workspace_locks import lock_workspace_nowait, retry_nowait
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import ConflictError, NotFoundException


def activity_enabled() -> None:
    """Keep deployment of the new metadata API separate from schema/application rollout."""
    if not get_settings().sync_observability_enabled:
        raise NotFoundException("Synchronization activity")


@asynccontextmanager
async def activity_boundary(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    *,
    write: bool = False,
) -> AsyncIterator[tuple[SyncInstallation, Workspace, FeedFence]]:
    """Fence current login/client/seat/reach while allowing post-revocation diagnostics."""
    activity_enabled()
    async with feed_fence(db) as fence:
        try:
            await locked(db, User, User.user_id == principal.user_id)
            # Reads share the row with each other and with foreign-key checks;
            # ownership, epoch, role and deletion writers still exclude them.
            # Holding the users row, the boundary retries instead of waiting.
            workspace = await lock_workspace_nowait(db, wid, shared=not write)
            live = await load_sync_identity(
                db,
                principal.token_hash,
                "integrations:manage" if write else "workspaces:read",
                lock=True,
            )
            if (live.user_id, live.client_id) != (principal.user_id, principal.client_id):
                raise NotFoundException("Synchronization activity")
            row = await locked(
                db,
                SyncInstallation,
                SyncInstallation.id == iid,
                SyncInstallation.workspace_id == wid,
            )
            if row is None:
                raise NotFoundException("Synchronization activity")
            service = await locked(db, SyncService, SyncService.service_id == row.service_id)
            if service is None or service.browser_client_id != live.client_id:
                raise NotFoundException("Synchronization activity")
            bind_actor(human_actor(live.user_id, live.client_id), db, row)
            await lock_membership(
                db, live.user_id, row, write=write, capacity=CapacityPolicy.ALLOWED
            )
            fence.deadlines.append(live.expires_at)
            yield row, workspace, fence
        except BaseException:
            await db.rollback()
            raise


async def overview(db: AsyncSession, wid: UUID, iid: UUID, principal: SyncPrincipal) -> dict:
    """Expose authoritative allowance and control state without charging or starting a period."""
    async with activity_boundary(db, wid, iid, principal) as (installation, _workspace, _fence):
        managed = await db.get(SyncManagedTarget, iid)
        feeds = list(
            (await db.scalars(select(SyncFeed).where(SyncFeed.installation_id == iid))).all()
        )
        records = await db.scalar(
            select(func.count())
            .select_from(SyncRecord)
            .join(SyncFeed)
            .where(
                SyncFeed.installation_id == iid,
                SyncRecord.payload.is_not(None),
            )
        )
        allowance = await read_public_usage_budget(db, wid)
        reached = None
        if allowance.limit and allowance.used is not None and allowance.period_seq is not None:
            reached = max(
                (
                    level
                    for level in (80, 90, 100)
                    if allowance.used * 100 >= allowance.limit * level
                ),
                default=None,
            )
        result = {
            "contract_version": SCHEMA,
            "installation_id": str(iid),
            "workspace_id": str(wid),
            "state": installation.state,
            "generation": installation.generation,
            "management_mode": managed.mode
            if managed
            else ("feed" if get_settings().sync_managed_enabled else None),
            "management_epoch": managed.epoch if managed else None,
            "stop_watermark": managed.stop_event_id if managed else None,
            "feed_count": len(feeds),
            "record_count": records or 0,
            "allowance": allowance.model_dump(mode="json"),
            "notification": {
                "thresholds": [80, 90, 100],
                "reached": reached,
                "dedupe_key": f"{wid}:{allowance.period_seq}:{reached}" if reached else None,
                "delivery_owner": "public",
            },
            "as_of": to_utc_iso(allowance.as_of),
        }
        await db.commit()
    return result


async def feed_configuration(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    *,
    feed_id: UUID | None = None,
) -> dict:
    """Observe opaque current configuration without a data grant or a source URL."""
    async with activity_boundary(db, wid, iid, principal) as (installation, _workspace, _fence):
        # Exclude a concurrent schema insertion through its Resource FK, as the
        # write path does: only FOR UPDATE conflicts with the insert's FOR KEY
        # SHARE, so two feed reads take turns on this row. Holding the users
        # row, the read retries a short holder (another feed read) instead of
        # waiting (#2914).
        async def lock_resource() -> Resource | None:
            return await db.scalar(
                select(Resource)
                .where(Resource.id == installation.resource_pk)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )

        resource = await retry_nowait(db, lock_resource)
        schema = await current_schema(db, installation) if resource is not None else None
        query = select(SyncFeed).where(SyncFeed.installation_id == iid)
        if feed_id is not None:
            query = query.where(SyncFeed.id == feed_id)
        rows = list(
            (
                await db.scalars(
                    query.order_by(SyncFeed.id).limit(21).execution_options(populate_existing=True)
                )
            ).all()
        )
        if feed_id is not None and not rows:
            raise NotFoundException("Synchronization feed")
        if len(rows) > 20:
            raise ConflictError("Synchronization feed limit exceeded")
        snapshot = {
            "workspace_id": str(wid),
            "installation_id": str(iid),
            "generation": installation.generation,
            "schema_hash": digest(schema.field_definitions) if schema else None,
            "schema_version": schema.schema_version if schema else None,
            "as_of": to_utc_iso(utcnow()),
        }
        items = [
            {
                "feed_id": str(row.id),
                "kind": row.kind,
                "config_hash": row.config_hash,
                "config_version": row.config_version,
                "revision": row.revision,
            }
            for row in rows
        ]
        result = {**snapshot, **items[0]} if feed_id is not None else {**snapshot, "items": items}
        await db.commit()
    return result


async def _run(db: AsyncSession, iid: UUID, rid: UUID) -> tuple[SyncRun, SyncFeed, int | None]:
    pair = (
        await db.execute(
            select(SyncRun, SyncFeed, func.jsonb_array_length(SyncRun.diff))
            .options(defer(SyncRun.diff))
            .join(SyncFeed)
            .where(SyncRun.id == rid, SyncFeed.installation_id == iid)
        )
    ).first()
    if pair is None:
        raise NotFoundException("Synchronization run")
    return pair[0], pair[1], pair[2]


def metadata_run(row: SyncRun, changes: int | None) -> dict:
    """Only scalar metadata is loaded; a potentially huge diff stays in PostgreSQL."""
    reference = row.manifest.get("manual_request_id")
    return {
        "run_id": str(row.id),
        "feed_id": str(row.feed_id),
        "operation_id": str(row.operation_id),
        "state": "expired"
        if row.state in {"uploading", "prepared"} and utcnow() >= row.expires_at
        else row.state,
        "changes": changes,
        "requires_approval": row.requires_approval,
        "expires_at": to_utc_iso(row.expires_at),
        "prepared_until": to_utc_iso(row.prepared_until),
        "origin": "manual" if reference else "unattributed",
        "manual_request_id": reference,
        "declared_records": row.manifest.get("records"),
        "declared_chunks": row.manifest.get("chunks"),
        "accepted_at": to_utc_iso(row.created_at),
        "committed_at": to_utc_iso(row.committed_at),
        "search": {"state": "not_evaluated", "reason": None},
    }


async def runs_page(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    *,
    limit: int,
    cursor: UUID | None,
) -> dict:
    """Bound memory to metadata, not limit times the largest possible diff."""
    async with activity_boundary(db, wid, iid, principal):
        query = (
            select(SyncRun, func.jsonb_array_length(SyncRun.diff))
            .options(
                defer(SyncRun.diff),
            )
            .join(SyncFeed)
            .where(SyncFeed.installation_id == iid)
        )
        if cursor is not None:
            prior = (
                await db.execute(
                    select(SyncRun.created_at, SyncRun.id)
                    .join(SyncFeed)
                    .where(
                        SyncRun.id == cursor,
                        SyncFeed.installation_id == iid,
                    )
                )
            ).first()
            if prior is None:
                raise NotFoundException("Synchronization run")
            query = query.where(
                or_(
                    SyncRun.created_at < prior.created_at,
                    and_(SyncRun.created_at == prior.created_at, SyncRun.id < prior.id),
                )
            )
        rows = (
            await db.execute(
                query.order_by(SyncRun.created_at.desc(), SyncRun.id.desc()).limit(limit + 1)
            )
        ).all()
        result = {
            "items": [metadata_run(row[0], row[1]) for row in rows[:limit]],
            "next_cursor": str(rows[limit - 1][0].id) if len(rows) > limit else None,
            "as_of": to_utc_iso(utcnow()),
        }
        await db.commit()
    return result


async def run_detail(
    db: AsyncSession, wid: UUID, iid: UUID, rid: UUID, principal: SyncPrincipal
) -> dict:
    """Check current index evidence without loading the run's stored diff or failure messages."""
    async with activity_boundary(db, wid, iid, principal):
        row, feed, changes = await _run(db, iid, rid)
        result = metadata_run(row, changes)
        state = await index_status(db, row, feed)
        result["search"] = {
            "state": state,
            "reason": {"failed": "index_not_current", "pending": "index_pending"}.get(state),
        }
        counts = (
            await db.execute(
                select(func.count(), func.coalesce(func.sum(SyncChunk.record_count), 0)).where(
                    SyncChunk.run_id == row.id,
                )
            )
        ).one()
        result.update(
            {
                "received_chunks": counts[0],
                "received_records": counts[1],
                "as_of": to_utc_iso(utcnow()),
            }
        )
        await db.commit()
    return result


async def activity_events(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    *,
    limit: int,
    before_id: int | None,
) -> dict:
    """Current target/service scope only; legacy arbitrary metadata is never republished."""
    async with activity_boundary(db, wid, iid, principal) as (row, _workspace, _fence):
        query = select(AuditLog).where(
            AuditLog.resource.in_(
                ("sync:" + str(iid), "sync-service:" + row.service_id, "sync-workspace:" + str(wid))
            ),
            AuditLog.user_metadata["schema"].as_string() == SCHEMA,
            AuditLog.action.in_(ACTIONS),
            AuditLog.user_metadata["service_id"].as_string() == row.service_id,
            or_(
                AuditLog.user_metadata["workspace_id"].as_string() == str(wid),
                and_(
                    AuditLog.resource == "sync-service:" + row.service_id,
                    AuditLog.user_metadata["workspace_id"].as_string().is_(None),
                ),
            ),
            AuditLog.created_at >= utcnow() - timedelta(days=RETENTION_DAYS),
        )
        if before_id is not None:
            query = query.where(AuditLog.id < before_id)
        rows = list((await db.scalars(query.order_by(AuditLog.id.desc()).limit(limit + 1))).all())
        items = [event_view(event) for event in rows[:limit]]
        ids = [UUID(item["manual_request_id"]) for item in items if item["manual_request_id"]]
        requests = (
            {
                str(req.id): req
                for req in (
                    await db.scalars(
                        select(SyncManualRequest).where(
                            SyncManualRequest.id.in_(ids),
                            SyncManualRequest.installation_id == iid,
                        )
                    )
                ).all()
            }
            if ids
            else {}
        )
        from services.erasure_details import erasure_pseudonym

        for item in items:
            req = requests.get(item["manual_request_id"])
            item["requested_by_ref"] = (
                erasure_pseudonym(req.requested_by) if req and req.requested_by else None
            )
        result = {
            "items": items,
            "next_before_id": rows[limit - 1].id if len(rows) > limit else None,
            "retention_days": RETENTION_DAYS,
            "as_of": to_utc_iso(utcnow()),
        }
        await db.commit()
    return result
