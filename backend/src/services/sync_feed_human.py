"""Human configuration, finite deletion consent and bounded canonical projection."""

import base64
import json
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal
from models.management_schemas import digest
from models.resource import ResourceSchema
from models.sync_credential import SyncCredentialClient
from models.sync_feed import SyncFeed, SyncRecord, SyncRun
from models.sync_feed_schemas import (
    DEFINITIONS,
    FAQ,
    PRODUCT,
    SCHEMA_HASH,
    ConfirmDiff,
    FeedConfig,
    FeedRecord,
)
from models.sync_installation import SyncService
from services.sync_feed_authority import current_schema, human_feed_boundary, locked
from services.sync_feed_runs import run_view
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import ConflictError, NotFoundException


async def configure_feed(
    db: AsyncSession, wid: UUID, iid: UUID, fid: UUID, principal: SyncPrincipal, body: FeedConfig
) -> dict:
    """A current editor explicitly approves the fixed public schema and source hash."""
    async with human_feed_boundary(db, wid, iid, principal, write=True) as (
        installation,
        _workspace,
        _fence,
    ):
        schema = await current_schema(db, installation)
        if schema is None:
            db.add(
                ResourceSchema(
                    resource_pk=installation.resource_pk,
                    resource_id=installation.resource_id,
                    schema_version=1,
                    field_definitions=DEFINITIONS,
                )
            )
        elif digest(schema.field_definitions) != SCHEMA_HASH:
            raise ConflictError("Dedicated synchronization schema has changed")
        row = await locked(db, SyncFeed, SyncFeed.id == fid, write=True)
        if row is not None and row.installation_id != iid:
            raise NotFoundException("Synchronization feed")
        if row is None:
            count = (
                await db.scalar(
                    select(func.count())
                    .select_from(SyncFeed)
                    .where(SyncFeed.installation_id == iid)
                )
            ) or 0
            if count >= 20:
                raise ConflictError("Synchronization feed limit reached")
            row = SyncFeed(
                id=fid, installation_id=iid, kind=body.kind, config_hash=body.config_hash
            )
            db.add(row)
        else:
            if row.kind != body.kind:
                raise ConflictError("Feed kind is immutable")
            if row.config_hash != body.config_hash:
                row.config_hash = body.config_hash
                row.config_version += 1
        await db.flush()
        await db.commit()
        return {
            "feed_id": str(row.id),
            "kind": row.kind,
            "config_hash": row.config_hash,
            "config_version": row.config_version,
            "revision": row.revision,
            "schema_hash": SCHEMA_HASH,
            "schema_version": schema.schema_version if schema else 1,
        }


async def approve_diff(
    db: AsyncSession, wid: UUID, iid: UUID, rid: UUID, principal: SyncPrincipal, body: ConfirmDiff
) -> dict:
    """Only human write authority can confirm an exact, finite prepared diff."""
    async with human_feed_boundary(db, wid, iid, principal, write=True) as (
        installation,
        workspace,
        fence,
    ):
        row = await db.scalar(
            select(SyncRun)
            .join(SyncFeed)
            .where(SyncRun.id == rid, SyncFeed.installation_id == iid)
            .with_for_update(of=SyncRun, nowait=True)
        )
        feed = await db.get(SyncFeed, row.feed_id) if row else None
        schema = await current_schema(db, installation)
        service = await locked(db, SyncService, SyncService.service_id == installation.service_id)
        client = await locked(
            db,
            SyncCredentialClient,
            SyncCredentialClient.client_id == installation.service_client_id,
        )
        if row is None or feed is None:
            raise NotFoundException("Synchronization run")
        if (
            row.state != "prepared"
            or row.diff_hash != body.diff_hash
            or body.approval_id is not None
            or row.prepared_until is None
            or utcnow() >= min(row.prepared_until, row.expires_at)
            or feed.config_hash != row.manifest["config_hash"]
            or feed.config_version != row.manifest["config_version"]
            or feed.revision != row.base_revision
            or schema is None
            or digest(schema.field_definitions) != SCHEMA_HASH
            or schema.schema_version != row.manifest["schema_version"]
            or service is None
            or client is None
            or not client.enabled
            or row.generations != [service.generation, client.generation, installation.generation]
        ):
            raise ConflictError("Prepared synchronization diff changed or expired")
        row.approval_id = row.approval_id or uuid4()
        row.approved_by, row.approval_epoch = principal.user_id, workspace.ownership_epoch
        fence.deadlines.extend([row.expires_at, row.prepared_until])
        await db.flush()
        await db.commit()
        return {
            "approval_id": str(row.approval_id),
            "diff_hash": row.diff_hash,
            "expires_at": to_utc_iso(row.prepared_until),
        }


def decode_cursor(value: str | None, revision: str) -> UUID | None:
    """A cursor is pagination input, never an authorization credential."""
    if value is None:
        return None
    try:
        if len(value) > 512:
            raise ValueError
        parsed = json.loads(base64.urlsafe_b64decode(value.encode()))
        if not isinstance(parsed, dict) or set(parsed) != {"revision", "after"}:
            raise ValueError
        after = UUID(parsed["after"])
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(400, "invalid_cursor") from None
    if parsed["revision"] != revision:
        raise ConflictError("Synchronization projection changed; restart pagination")
    return after


async def project_records(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    *,
    limit: int,
    cursor: str | None,
    feed_id: UUID | None,
) -> dict:
    """Explicitly scoped, current-member data reads; never spends public MCP budget."""
    async with human_feed_boundary(db, wid, iid, principal) as (installation, _workspace, _fence):
        schema = await current_schema(db, installation)
        if schema is None or digest(schema.field_definitions) != SCHEMA_HASH:
            raise ConflictError("Approved synchronization schema changed")
        feeds = (
            await db.scalars(
                select(SyncFeed).where(SyncFeed.installation_id == iid).order_by(SyncFeed.id)
            )
        ).all()
        if feed_id is not None and feed_id not in {feed.id for feed in feeds}:
            raise NotFoundException("Synchronization feed")
        revision = digest(
            {
                "workspace": str(wid),
                "installation": str(iid),
                "filter": str(feed_id),
                "schema": SCHEMA_HASH,
                "feeds": [
                    (str(feed.id), feed.revision, feed.config_hash, feed.config_version)
                    for feed in feeds
                ],
            }
        )
        after = decode_cursor(cursor, revision)
        query = (
            select(SyncRecord, SyncFeed.kind)
            .join(SyncFeed)
            .where(
                SyncFeed.installation_id == iid,
                SyncRecord.resource_pk == installation.resource_pk,
                SyncRecord.payload.is_not(None),
            )
        )
        if feed_id is not None:
            query = query.where(SyncRecord.feed_id == feed_id)
        if after is not None:
            query = query.where(SyncRecord.id > after)
        rows = (await db.execute(query.order_by(SyncRecord.id).limit(limit + 1))).all()
        records = []
        for row, kind in rows[:limit]:
            payload = row.payload or {}
            allowed = PRODUCT if kind == "product" else FAQ
            if (
                payload.get("record_type") != kind
                or payload.get("external_id") != row.external_id
                or not set(payload).issubset(allowed | {"record_type", "external_id"})
            ):
                raise ConflictError("Approved synchronization record changed")
            fields = {key: value for key, value in payload.items() if key in allowed}
            FeedRecord(external_id=row.external_id, fields=fields).public_payload(kind)
            records.append(
                {
                    "feed_id": str(row.feed_id),
                    "kind": kind,
                    "external_id": row.external_id,
                    "version": row.version,
                    "fields": fields,
                }
            )
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps({"revision": revision, "after": str(rows[limit - 1][0].id)}).encode()
            ).decode()
            if len(rows) > limit
            else None
        )
        await db.commit()
        return {"records": records, "next_cursor": next_cursor, "revision": revision}


async def human_diff(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    rid: UUID,
    principal: SyncPrincipal,
    *,
    offset: int,
    limit: int,
) -> dict:
    """A bounded metadata-only deletion preview under the human data-read scope."""
    async with human_feed_boundary(db, wid, iid, principal) as (installation, _workspace, _fence):
        schema = await current_schema(db, installation)
        if schema is None or digest(schema.field_definitions) != SCHEMA_HASH:
            raise ConflictError("Approved synchronization schema changed")
        row = await db.scalar(
            select(SyncRun).join(SyncFeed).where(SyncRun.id == rid, SyncFeed.installation_id == iid)
        )
        if row is None:
            raise NotFoundException("Synchronization run")
        result = {
            **run_view(row),
            "diff": (row.diff or [])[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(row.diff or []) else None,
        }
        await db.commit()
        return result
