"""Shared Resource batch-ingest domain service.

Issue #1255: REST ``api/routes/resource_ingest.py::ingest_batch`` and MCP
``mcp_server/tools/resource.py::handle_ingest_events`` previously implemented
the same domain pipeline independently. They are now thin adapters over this
module: the adapters keep authentication, authorization, wire parsing, and
response/error serialization; this service owns per-event domain validation,
authoritative Resource resolution, per-event SAVEPOINT persistence with
constraint classification, the commit boundary, and post-commit indexer
scheduling.

Design notes:
    - The service returns *structured* item errors (``IngestItemError`` with a
      ``kind`` plus formatting params). Each adapter renders its own historic
      wire strings from them, so the REST response schema and the MCP envelope
      stay byte-compatible with their pre-refactor shapes.
    - Payload size is measured in UTF-8 **bytes** (the semantic the constant
      ``MAX_PAYLOAD_SIZE_BYTES`` names). The REST path previously counted
      ``str`` characters, which under-counted multibyte payloads.
    - Quota enforcement stays in the adapters via the already-shared
      ``services.resource_quota_service`` helpers (both surfaces reserve
      against the same workspace-scoped Redis counter before any write).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from utils.logger import get_logger

logger = get_logger(__name__)

MAX_BATCH_SIZE = 100
MAX_PAYLOAD_SIZE_BYTES = 100_000
DEFAULT_IMPORTANCE = 0.6  # Issue #262

# Structured error kinds. Adapters map these to their historic wire strings —
# never serialize the kind itself (or raw DB constraint details) to clients.
KIND_NOT_AN_OBJECT = "not_an_object"
KIND_INVALID_OP = "invalid_op"
KIND_MISSING_DOC_ID = "missing_doc_id"
KIND_PAYLOAD_REQUIRED = "payload_required"
KIND_VERSION_NOT_INT = "version_not_int"
KIND_VERSION_TOO_SMALL_UPSERT = "version_too_small_upsert"
KIND_PAYLOAD_TOO_LARGE = "payload_too_large"
KIND_IMPORTANCE_NOT_NUMBER = "importance_not_number"
KIND_IMPORTANCE_OUT_OF_RANGE = "importance_out_of_range"
KIND_PAYLOAD_NOT_NULL_DELETE = "payload_not_null_delete"
KIND_VERSION_TOO_SMALL = "version_too_small"
KIND_IDEMPOTENCY_INVALID = "idempotency_invalid"
KIND_DUPLICATE_VERSION = "duplicate_version"
KIND_DUPLICATE_IDEMPOTENCY = "duplicate_idempotency"
KIND_CONSTRAINT_VIOLATION = "constraint_violation"
KIND_UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class IngestEventInput:
    """One normalized, domain-valid event ready for persistence."""

    index: int
    op: str
    doc_id: str
    version: int | None
    payload: dict | None
    idempotency_key: str | None
    event_metadata: dict | None
    importance: float


@dataclass(frozen=True)
class IngestItemError:
    """Structured per-item failure; adapters own the wire message."""

    index: int
    kind: str
    doc_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestBatchResult:
    """Outcome of the persistence pass."""

    created_ids: list[int] = field(default_factory=list)
    errors: list[IngestItemError] = field(default_factory=list)


def validate_events(
    raw_events: list[Any],
) -> tuple[list[IngestEventInput], list[IngestItemError]]:
    """Validate raw event dicts into normalized inputs.

    Implements the domain rules both surfaces share. REST inputs arrive
    Pydantic-validated (``ResourceEventRequest``), so for that adapter this
    pass only adds the byte-accurate payload-size check; the MCP adapter
    relies on it for all per-item validation. Check order is preserved from
    the historic MCP implementation so first-error precedence is unchanged
    (op → doc_id → upsert payload/version → payload size → importance →
    delete rules).

    An explicit ``importance: null`` is treated as absent and takes
    ``DEFAULT_IMPORTANCE`` (the REST semantic; the MCP path previously
    rejected an explicit null — unified per #1255's equivalence criterion).
    """
    valid: list[IngestEventInput] = []
    errors: list[IngestItemError] = []

    for i, event_data in enumerate(raw_events):
        if not isinstance(event_data, dict):
            errors.append(IngestItemError(index=i, kind=KIND_NOT_AN_OBJECT))
            continue

        op = event_data.get("op")
        doc_id = event_data.get("doc_id")

        if op not in ("upsert", "delete"):
            errors.append(IngestItemError(index=i, kind=KIND_INVALID_OP, detail={"op": op}))
            continue
        if not doc_id:
            errors.append(IngestItemError(index=i, kind=KIND_MISSING_DOC_ID))
            continue

        payload = event_data.get("payload")
        version: int | None = None

        if op == "upsert":
            if not payload:
                errors.append(IngestItemError(index=i, kind=KIND_PAYLOAD_REQUIRED, doc_id=doc_id))
                continue
            version = event_data.get("version")
            try:
                version = int(version) if version is not None else None
            except (ValueError, TypeError):
                errors.append(IngestItemError(index=i, kind=KIND_VERSION_NOT_INT, doc_id=doc_id))
                continue
            if version is None or version < 1:
                errors.append(
                    IngestItemError(index=i, kind=KIND_VERSION_TOO_SMALL_UPSERT, doc_id=doc_id)
                )
                continue

        if payload:
            payload_size = len(json.dumps(payload).encode("utf-8"))
            if payload_size > MAX_PAYLOAD_SIZE_BYTES:
                errors.append(
                    IngestItemError(
                        index=i,
                        kind=KIND_PAYLOAD_TOO_LARGE,
                        doc_id=doc_id,
                        detail={
                            "payload_size": payload_size,
                            "max": MAX_PAYLOAD_SIZE_BYTES,
                        },
                    )
                )
                continue

        importance_raw = event_data.get("importance")
        if importance_raw is None:
            importance = DEFAULT_IMPORTANCE
        else:
            try:
                importance = float(importance_raw)
            except (ValueError, TypeError):
                errors.append(
                    IngestItemError(index=i, kind=KIND_IMPORTANCE_NOT_NUMBER, doc_id=doc_id)
                )
                continue
            if importance < 0.0 or importance > 1.0:
                errors.append(
                    IngestItemError(index=i, kind=KIND_IMPORTANCE_OUT_OF_RANGE, doc_id=doc_id)
                )
                continue

        if op == "delete":
            if payload is not None:
                errors.append(
                    IngestItemError(index=i, kind=KIND_PAYLOAD_NOT_NULL_DELETE, doc_id=doc_id)
                )
                continue
            version = event_data.get("version")
            if version is not None:
                try:
                    version = int(version)
                except (ValueError, TypeError):
                    errors.append(
                        IngestItemError(index=i, kind=KIND_VERSION_NOT_INT, doc_id=doc_id)
                    )
                    continue
                if version < 1:
                    errors.append(
                        IngestItemError(index=i, kind=KIND_VERSION_TOO_SMALL, doc_id=doc_id)
                    )
                    continue

        valid.append(
            IngestEventInput(
                index=i,
                op=op,
                doc_id=doc_id,
                version=version,
                payload=payload if op == "upsert" else None,
                idempotency_key=event_data.get("idempotency_key"),
                event_metadata=event_data.get("event_metadata", {}),
                importance=importance,
            )
        )

    return valid, errors


async def resolve_authoritative_resource_pk(
    db: AsyncSession, *, workspace_id: UUID, resource_id: str
) -> UUID | None:
    """Resolve ``resources.id`` via the shared chokepoint (Issue #390 Phase 2).

    Returns ``None`` when the Resource entity row does not exist in the
    workspace — the adapter rejects the batch up front with an actionable
    error instead of letting the ``before_insert`` invariant listener raise a
    masked IntegrityError for every event.
    """
    from services.resource_lookup import resolve_resource_pk

    return await resolve_resource_pk(db, workspace_id, resource_id)


async def persist_events(
    db: AsyncSession,
    *,
    resource_id: str,
    resource_pk: UUID,
    events: list[IngestEventInput],
) -> IngestBatchResult:
    """Persist validated events with per-event SAVEPOINT isolation.

    Each event is inserted inside ``db.begin_nested()`` so an IntegrityError
    on one row cannot abort the outer transaction and break partial success
    for the sibling events. IntegrityErrors are classified by constraint name
    (``db.constraint_names``); raw constraint details are never placed in the
    structured error — only the classification and formatting params.

    A non-IntegrityError failure on one event is likewise recorded as a
    per-item ``KIND_UNEXPECTED`` error and processing continues (partial
    success; the pre-refactor MCP path failed the whole call here).

    Nothing is committed — see :func:`finalize_batch` for the commit and
    indexer boundary.
    """
    from db.constraint_names import (
        RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE,
        RESOURCE_EVENTS_UPSERT_UNIQUE,
        integrity_error_constraint_name,
    )
    from models.resource import ResourceEvent
    from services.connector_provisioning import (
        get_connector_id_for_resource_pk,
        validate_connector_idempotency_key,
    )
    from utils.exceptions import ValidationError

    connector_id = await get_connector_id_for_resource_pk(db, resource_pk)

    result = IngestBatchResult()

    for ev in events:
        try:
            try:
                validate_connector_idempotency_key(
                    connector_id=connector_id,
                    idempotency_key=ev.idempotency_key,
                )
            except ValidationError as ve:
                result.errors.append(
                    IngestItemError(
                        index=ev.index,
                        kind=KIND_IDEMPOTENCY_INVALID,
                        doc_id=ev.doc_id,
                        detail={"message": ve.message},
                    )
                )
                continue

            event = ResourceEvent(
                resource_id=resource_id,
                resource_pk=resource_pk,
                op=ev.op,
                doc_id=ev.doc_id,
                version=ev.version,
                payload=ev.payload,
                idempotency_key=ev.idempotency_key,
                event_metadata=ev.event_metadata,
                importance=ev.importance,
            )

            async with db.begin_nested():
                db.add(event)
                await db.flush()
            result.created_ids.append(event.id)

        except IntegrityError as ie:
            constraint = integrity_error_constraint_name(ie)
            if constraint == RESOURCE_EVENTS_UPSERT_UNIQUE:
                logger.debug(
                    "batch_event_duplicate_version_skipped",
                    doc_id=ev.doc_id,
                    version=ev.version,
                )
                result.errors.append(
                    IngestItemError(
                        index=ev.index,
                        kind=KIND_DUPLICATE_VERSION,
                        doc_id=ev.doc_id,
                        detail={"version": ev.version},
                    )
                )
            elif constraint == RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE:
                logger.debug(
                    "batch_event_duplicate_idempotency_key_skipped",
                    key=ev.idempotency_key,
                )
                result.errors.append(
                    IngestItemError(
                        index=ev.index,
                        kind=KIND_DUPLICATE_IDEMPOTENCY,
                        doc_id=ev.doc_id,
                    )
                )
            else:
                # Partial success: log the constraint name for triage; never
                # leak raw DB constraint details to clients.
                logger.error(
                    "batch_event_integrity_error_unhandled",
                    resource_id=resource_id,
                    index=ev.index,
                    doc_id=ev.doc_id,
                    constraint=constraint,
                )
                result.errors.append(
                    IngestItemError(
                        index=ev.index,
                        kind=KIND_CONSTRAINT_VIOLATION,
                        doc_id=ev.doc_id,
                        detail={"constraint": constraint},
                    )
                )

        except Exception as e:
            logger.error(
                "batch_event_unexpected_error",
                resource_id=resource_id,
                index=ev.index,
                error=str(e),
            )
            result.errors.append(
                IngestItemError(
                    index=ev.index,
                    kind=KIND_UNEXPECTED,
                    doc_id=ev.doc_id,
                    detail={"message": str(e)},
                )
            )

    return result


async def finalize_batch(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    resource_id: str,
    created_ids: list[int],
) -> None:
    """Commit the batch, then schedule the indexer from the post-commit boundary.

    The event rows are committed first so the batch's partial-success outcome
    is durable regardless of what indexer scheduling does; the IndexerState
    rows are then written and committed explicitly (the pre-refactor REST path
    relied on the ``get_db`` teardown auto-commit for them, and the MCP path
    scheduled before commit — this is the single, clearly defined boundary
    #1255 asks for). The indexer is scheduled only when at least one event was
    created.
    """
    await db.commit()

    if created_ids:
        # Lazy import from the routes module: it is the single home of the
        # scheduling helper (also used by the single-event path) and the
        # established patch target in the test suite. Moving it is out of
        # scope for #1255 ("no broad cleanup of unrelated Resource tools").
        from api.routes.resource_ingest import _schedule_indexer_for_resource

        await _schedule_indexer_for_resource(db, workspace_id, resource_id)
        await db.commit()
