"""Atomic canonical snapshot, append-only events and durable indexing receipt."""

import hashlib
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Workspace
from models.management_schemas import digest
from models.memory import Memory
from models.resource import ResourceEvent
from models.sync_feed import SyncFeed, SyncIndexOutbox, SyncRecord, SyncRun
from models.sync_feed_schemas import ConfirmDiff, FeedRecord, destructive
from services.sync_credentials import CredentialAuthority
from services.sync_feed_authority import FeedFence, lock_membership
from services.sync_feed_provenance import assert_managed_memories
from services.sync_feed_runs import build_diff, clear_staging, editable, run_for, staged_records
from utils.datetime import utcnow
from utils.exceptions import ConflictError


def document_id(iid: UUID, fid: UUID, external_id: str) -> str:
    """A fixed namespace; callers never supply internal document IDs or versions."""
    return "sync:" + hashlib.sha256(f"{iid}:{fid}:{external_id}".encode()).hexdigest()


async def owned_documents(
    db: AsyncSession, authority: CredentialAuthority, feed: SyncFeed, diff: list[dict]
) -> dict[str, SyncRecord]:
    """Reject foreign events and manually authored/edited memories before any write."""
    ids = [item["external_id"] for item in diff]
    if not ids:
        return {}
    previous = {
        item.external_id: item
        for item in (
            await db.scalars(
                select(SyncRecord)
                .where(SyncRecord.feed_id == feed.id, SyncRecord.external_id.in_(ids))
                .with_for_update(nowait=True)
            )
        ).all()
    }
    docs = {document_id(authority.installation.id, feed.id, eid): eid for eid in ids}
    latest = dict(
        (
            await db.execute(
                select(ResourceEvent.doc_id, func.max(ResourceEvent.id))
                .where(
                    ResourceEvent.resource_pk == authority.resource.id,
                    ResourceEvent.doc_id.in_(list(docs)),
                )
                .group_by(ResourceEvent.doc_id)
            )
        ).all()
    )
    for doc, external_id in docs.items():
        old = previous.get(external_id)
        if (old is None and doc in latest) or (
            old is not None
            and (
                old.doc_id != doc
                or old.resource_pk != authority.resource.id
                or latest.get(doc) != old.last_event_id
            )
        ):
            raise ConflictError("Synchronization document ownership changed")
    await assert_managed_memories(
        db, authority.context, authority.resource.resource_id, authority.resource.id, list(docs)
    )
    return previous


async def commit_run(
    db: AsyncSession, authority: CredentialAuthority, rid: UUID, body: ConfirmDiff, fence: FeedFence
) -> SyncRun:
    """The enclosing paid guard commits everything once, after all final checks."""
    row, feed = await run_for(db, authority, rid)
    if row.state == "committed":
        if digest(body.model_dump(mode="json")) != row.commit_hash:
            raise ConflictError("Commit identity was used with different content")
        return row
    await editable(db, authority, row, feed, fence)
    if (
        row.state != "prepared"
        or body.diff_hash != row.diff_hash
        or row.prepared_until is None
        or utcnow() >= row.prepared_until
    ):
        raise ConflictError("Prepare the current synchronization diff first")
    fence.deadlines.append(row.prepared_until)
    diff, previous_count = await build_diff(db, row, feed)
    if diff != row.diff:
        raise ConflictError("Prepared synchronization diff changed")
    if destructive(diff, previous_count):
        workspace = await db.get(Workspace, authority.installation.workspace_id)
        if (
            row.approval_id is None
            or body.approval_id != row.approval_id
            or row.approved_by is None
            or workspace is None
            or workspace.ownership_epoch != row.approval_epoch
        ):
            raise HTTPException(409, "human_approval_required")
        await lock_membership(db, row.approved_by, authority.installation, write=True)
    elif body.approval_id is not None and body.approval_id != row.approval_id:
        raise ConflictError("Unknown synchronization approval")
    previous = await owned_documents(db, authority, feed, diff)
    incoming = {
        item["external_id"]: FeedRecord.model_validate(item).public_payload(feed.kind)
        for item in await staged_records(db, row)
    }
    deleted_docs = [
        document_id(authority.installation.id, feed.id, change["external_id"])
        for change in diff
        if change["op"] == "delete"
    ]
    delete_versions: dict[str, set[int]] = {doc: set() for doc in deleted_docs}
    if deleted_docs:
        history = (
            await db.execute(
                select(Memory.resource_doc_id, Memory.resource_version)
                .where(
                    Memory.context_id == authority.context.id,
                    Memory.resource_id == authority.resource.resource_id,
                    Memory.resource_doc_id.in_(deleted_docs),
                    Memory.deleted_at.is_(None),
                )
                .limit(20001)
            )
        ).all()
        if len(history) + sum(change["op"] == "upsert" for change in diff) > 20000:
            raise ConflictError("Synchronization event limit exceeded")
        for doc, version in history:
            if version is not None:
                delete_versions[doc].add(version)
    events: list[tuple[dict, ResourceEvent]] = []
    # Release owned rows before new IDs consume capacity. Otherwise a full
    # workspace can stall forever at an upsert ahead of its own queued deletes.
    for change in sorted(diff, key=lambda item: item["op"] != "delete"):
        eid = change["external_id"]
        old = previous.get(eid)
        # Deletion addresses precisely the last feed-owned resource version.
        version = (
            old.version
            if change["op"] == "delete" and old is not None
            else (old.version + 1 if old else 1)
        )
        doc = document_id(authority.installation.id, feed.id, eid)
        versions = (
            sorted(delete_versions[doc] | {version}) if change["op"] == "delete" else [version]
        )
        for owned_version in versions:
            event = ResourceEvent(
                resource_pk=authority.resource.id,
                resource_id=authority.resource.resource_id,
                op=change["op"],
                doc_id=doc,
                version=owned_version,
                payload=incoming.get(eid),
                idempotency_key=f"sync:{row.id}:{doc}:{owned_version}",
                event_metadata={
                    "sync_run_id": str(row.id),
                    "sync_feed_id": str(feed.id),
                    "installation_generation": authority.installation.generation,
                },
            )
            db.add(event)
            events.append((change, event))
    if len(events) > 20000:
        raise ConflictError("Synchronization event limit exceeded")
    await db.flush()
    for change, event in events:
        assert event.version is not None  # Every feed event is version-specific.
        eid = change["external_id"]
        old = previous.get(eid)
        if old is None:
            db.add(
                SyncRecord(
                    feed_id=feed.id,
                    resource_pk=authority.resource.id,
                    external_id=eid,
                    doc_id=event.doc_id,
                    version=event.version,
                    payload=incoming.get(eid),
                    last_event_id=event.id,
                )
            )
        else:
            old.payload = incoming.get(eid)
            old.version = event.version
            old.last_event_id = event.id
    if events:
        db.add(
            SyncIndexOutbox(
                run_id=row.id,
                resource_pk=authority.resource.id,
                context_id=authority.context.id,
                last_event_id=events[-1][1].id,
                event_ids=[event.id for _change, event in events],
            )
        )
    feed.revision += 1
    row.state = "committed"
    row.committed_at = utcnow()
    row.commit_hash = digest(body.model_dump(mode="json"))
    await clear_staging(db, row)
    await db.flush()
    return row
