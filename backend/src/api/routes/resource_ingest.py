"""Resource Ingest API routes.

Issue #238: Resource-driven incremental indexing for Public Contexts.

Provides endpoints for external systems (EC inventory, etc.) to push events.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import services.resource_ingest_service as resource_ingest_service
from auth.resource_tokens import ResourceTokenManager
from db.base import get_db
from db.constraint_names import (
    RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE,
    RESOURCE_EVENTS_UPSERT_UNIQUE,
    integrity_error_constraint_name,
)
from models.auth import Context
from models.resource import Resource, ResourceEvent, ResourceToken, WorkspaceConnector
from models.schemas import (
    ResourceEventBatchRequest,
    ResourceEventBatchResponse,
    ResourceEventRequest,
    ResourceEventResponse,
)
from services.connector_provisioning import (
    get_connector_id_for_resource_pk,
    validate_connector_idempotency_key,
)
from services.permission_service import PermissionService
from services.resource_ingest_service import IngestItemError
from services.resource_lookup import resolve_resource_pk
from services.resource_quota_service import check_event_quota
from utils.datetime import utcnow
from utils.exceptions import AuthorizationError, ConflictError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/resources", tags=["resource-ingest"])

# Maximum payload size (100KB) — single source in the shared ingest service
# (Issue #1255); the single-event path below reads the same constant.
MAX_PAYLOAD_SIZE_BYTES = resource_ingest_service.MAX_PAYLOAD_SIZE_BYTES


def _format_batch_item_error(err: IngestItemError) -> dict:
    """Render a structured batch item error in the historic REST wire shape.

    The strings are byte-compatible with the pre-#1255 in-handler messages;
    every REST item error carries ``doc_id``.
    """
    kind = err.kind
    if kind == resource_ingest_service.KIND_PAYLOAD_TOO_LARGE:
        message = f"Payload too large: {err.detail['payload_size']} bytes"
    elif kind == resource_ingest_service.KIND_IDEMPOTENCY_INVALID:
        message = err.detail["message"]
    elif kind == resource_ingest_service.KIND_DUPLICATE_VERSION:
        message = f"Duplicate version {err.detail['version']}"
    elif kind == resource_ingest_service.KIND_DUPLICATE_IDEMPOTENCY:
        message = "Duplicate idempotency key"
    elif kind == resource_ingest_service.KIND_CONSTRAINT_VIOLATION:
        message = "Database constraint violation"
    elif kind == resource_ingest_service.KIND_UNEXPECTED:
        message = err.detail["message"]
    else:
        # Validation kinds are unreachable on this surface (Pydantic already
        # rejected the request with 422); keep a defensive generic message.
        message = "Invalid event"
    return {"index": err.index, "doc_id": err.doc_id, "error": message}


# ============================================================================
# Dependency: Resource Token Authentication
# ============================================================================


async def verify_resource_token(
    resource_id: str,
    request: Request,
    x_resource_api_key: str | None = Header(None, alias="X-Resource-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> tuple[ResourceToken, int, Context | SimpleNamespace]:
    """Verify resource token and enforce workspace boundary.

    See SECURITY.md (2026-04-14 advisory) for the threat model.

    Returns:
        (token_record, quota_events_per_hour, context)

    Raises:
        HTTPException 401: missing/invalid/revoked token
        HTTPException 403: token creator is not a member of the Context's workspace
        HTTPException 404: resource_id is not bound to any active Context
        HTTPException 409: resource_id is ambiguous across active Context bindings
            (only reachable pre-migration; see `_resolve_authoritative_context`)
    """
    if not x_resource_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Resource-API-Key header required",
        )

    manager = ResourceTokenManager(db)
    token_record = await manager.verify_token(x_resource_api_key, resource_id)

    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked resource token",
        )

    context = await _resolve_authoritative_context(db, resource_id)
    if context is None:
        connector_workspace_id = await _resolve_connector_workspace_id(
            db,
            token_record=token_record,
            resource_id=resource_id,
        )
        if connector_workspace_id is None:
            logger.warning(
                "resource_id_unbound_on_ingest",
                resource_id=resource_id,
                token_id=token_record.id,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Resource is not bound to an active context or connector",
            )
        context = SimpleNamespace(
            id=None,
            resource_id=resource_id,
            workspace_id=connector_workspace_id,
        )

    await _enforce_workspace_membership(db, request, token_record, context)

    return token_record, token_record.quota_events_per_hour, context


async def _resolve_authoritative_context(
    db: AsyncSession,
    resource_id: str,
) -> Context | None:
    """Return the single active Context for resource_id, if any.

    After migration a96, the partial UNIQUE index
    ``ux_contexts_resource_id_active`` guarantees at most one active row
    per resource_id. This helper defends against the window before that
    migration runs: if pre-existing cross-workspace collisions are still
    in the table, we fail closed (409) rather than silently picking one
    row or bubbling an opaque 500.

    Returns:
        Single active Context, or None if no match.

    Raises:
        HTTPException 409: multiple active contexts share the resource_id
            (only reachable pre-migration; run a96 to eliminate).
    """
    # LIMIT 2 is enough to distinguish 0 / 1 / ambiguous — we do not need to
    # know how many total duplicates exist, just that there are at least two.
    result = await db.execute(
        select(Context)
        .where(
            Context.resource_id == resource_id,
            Context.deleted_at.is_(None),
        )
        .limit(2)
    )
    rows = result.scalars().all()
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            "resource_id_ambiguous_on_ingest",
            resource_id=resource_id,
            match_count=len(rows),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "resource_id is ambiguous — multiple active contexts share "
                "this identifier. Contact an administrator to resolve the "
                "collision."
            ),
        )
    return rows[0]


async def _resolve_connector_workspace_id(
    db: AsyncSession,
    *,
    token_record: ResourceToken,
    resource_id: str,
) -> UUID | None:
    """Resolve workspace for connector-owned resources without Context rows."""
    if token_record.resource_pk is None:
        return None

    return (
        await db.execute(
            select(Resource.workspace_id)
            .join(WorkspaceConnector, WorkspaceConnector.resource_pk == Resource.id)
            .where(
                Resource.id == token_record.resource_pk,
                Resource.resource_id == resource_id,
            )
        )
    ).scalar_one_or_none()


async def _enforce_workspace_membership(
    db: AsyncSession,
    request: Request,
    token_record: ResourceToken,
    context: Context,
) -> None:
    """Reject ingest unless token creator has active workspace access.

    Uses ``PermissionService.check_workspace_access`` which enforces:
    (a) the workspace exists and is not soft-deleted, and
    (b) the token creator is still a ``WorkspaceMember``.
    ``is_workspace_member`` was insufficient because it skipped (a) —
    a deleted workspace would have left ingest open on lingering tokens.

    Does NOT use ``User.current_workspace_id``, which is a mutable UI
    preference and cannot be trusted for authorization.
    """
    if not token_record.created_by:
        logger.warning(
            "resource_ingest_missing_token_creator",
            resource_id=context.resource_id,
            token_id=token_record.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token is missing creator attribution and cannot be authorized.",
        )

    permissions = PermissionService(db)
    try:
        await permissions.check_workspace_access(
            user_id=token_record.created_by,
            workspace_id=context.workspace_id,
            required_role="member",
        )
    except AuthorizationError as auth_error:
        logger.warning(
            "cross_tenant_ingest_attempt",
            resource_id=context.resource_id,
            token_id=token_record.id,
            target_workspace_id=str(context.workspace_id),
            token_creator=token_record.created_by,
            client_ip=request.client.host if request.client else None,
            reason=auth_error.reason or "workspace_access_denied",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Resource ingest denied: the token's creator does not have "
                "active access to the workspace that owns this resource."
            ),
        ) from auth_error

    # Issue #390 Phase 2 — close the CWE-639 auth-boundary variant
    # (Copilot catches on PR #392 loops 7, 9, 10). Even after verify_token
    # pins the token to a single Resource via the resource_pk JOIN, the
    # slug-reused case can let the ingest pipeline:
    #   1) verify T pinned to workspace A's Resource (via resource_pk)
    #   2) resolve_authoritative_context return workspace B's Context
    #      (A's Context soft-deleted, B's Context active, same slug)
    #   3) enforce_workspace_membership pass because the token creator is
    #      ALSO a member of workspace B
    #   4) handler write the event with T.resource_pk = A's Resource ID,
    #      despite the request looking like a workspace-B operation
    #
    # The fix: resolve the authoritative workspace via ``Resource.workspace_id``
    # (via token's ``resource_pk`` FK) and reject if it does not match the
    # Context's workspace. The token's own ``workspace_id`` column is still
    # a Phase 1 nullable shadow — relying on it alone would let legacy
    # NULL-workspace_id tokens bypass the check (loop 10 catch). The FK
    # ``resource_pk → Resource`` is the authoritative binding.
    resource_workspace_id: UUID | None = None
    if token_record.resource_pk is not None:
        resource_workspace_id = (
            await db.execute(
                select(Resource.workspace_id).where(Resource.id == token_record.resource_pk)
            )
        ).scalar_one_or_none()
    elif token_record.workspace_id is not None:
        # Fallback: legacy token with resource_pk IS NULL but workspace_id
        # populated (transient state between a97 workspace_id backfill and
        # b01 resource_pk backfill). Use the shadow column directly.
        resource_workspace_id = token_record.workspace_id

    if resource_workspace_id is not None and resource_workspace_id != context.workspace_id:
        logger.warning(
            "cross_tenant_ingest_token_workspace_mismatch",
            resource_id=context.resource_id,
            token_id=token_record.id,
            token_workspace_id=str(resource_workspace_id),
            context_workspace_id=str(context.workspace_id),
            token_creator=token_record.created_by,
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Resource ingest denied: the token's workspace does not match "
                "the context's workspace for this resource."
            ),
        )


# ============================================================================
# Resource Ingest Endpoints
# ============================================================================


@router.post(
    "/{resource_id}/events",
    response_model=ResourceEventResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_event(
    resource_id: str,
    request: ResourceEventRequest,
    auth: tuple[ResourceToken, int, Any] = Depends(verify_resource_token),
    db: AsyncSession = Depends(get_db),
):
    """Ingest a single resource event (upsert or delete).

    Issue #238: Append-only event log for incremental indexing.

    The ``resource_id`` URL path parameter accepts the human-readable slug
    (e.g. ``my-github-repo``). Internally, token verification resolves the
    request context from that identifier (see ``verify_resource_token`` and
    ``_resolve_authoritative_context``), and workspace access is enforced as
    a separate validation step. The authoritative resource record used for
    writes is then taken from the verified token record
    (``token_record.resource_pk``), so external callers never need to know or
    supply UUIDs.

    Security:
        - Requires X-Resource-API-Key header
        - Token must be scoped to this resource_id
        - Rate limited by quota_events_per_hour

    Request:
        POST /api/v1/resources/{resource_id}/events
        Headers:
            X-Resource-API-Key: kagura_resource_...
        Body:
            {
                "op": "upsert",
                "doc_id": "PROD-12345",
                "version": 3,
                "payload": {"product_name": "...", "price": 5980},
                "idempotency_key": "optional-key"
            }

    Response:
        {
            "status": "success",
            "event_id": 12345,
            "queued": true,
            "estimated_indexing_time_seconds": 600
        }

    Errors:
        - 401: Invalid/revoked token
        - 409: Duplicate version (resource_id, doc_id, version already exists)
        - 413: Payload too large (>100KB)
        - 422: Validation error (e.g., missing payload for upsert)
        - 429: Quota exceeded
    """
    # Context is resolved and workspace-verified in the dependency — always non-None here.
    token_record, quota_per_hour, context = auth
    connector_id = await get_connector_id_for_resource_pk(db, token_record.resource_pk)
    validate_connector_idempotency_key(
        connector_id=connector_id,
        idempotency_key=request.idempotency_key,
    )

    logger.info(
        "resource_event_ingest_started",
        resource_id=resource_id,
        op=request.op,
        doc_id=request.doc_id,
        version=request.version,
        has_payload=request.payload is not None,
    )

    # 1. Check quota (events per hour) — counter is workspace-scoped and shared
    # with the MCP ingest path so combined traffic counts against one ceiling.
    await check_event_quota(resource_id, context.workspace_id, quota_per_hour)

    # 2. Validate payload size
    if request.payload:
        import json

        payload_size = len(json.dumps(request.payload))
        if payload_size > MAX_PAYLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Payload too large: {payload_size} bytes (max {MAX_PAYLOAD_SIZE_BYTES})",
            )

    # 3. Create event record
    #
    # ``resource_pk`` is sourced from the auth token (backfilled by migration
    # a97 for existing tokens) so the partial UNIQUE
    # ``WHERE op='upsert' AND resource_pk IS NOT NULL`` actually applies.
    # If the token was issued before a97 and never backfilled, we pass
    # ``None`` — ingest still works via the legacy ``resource_id`` column,
    # the partial UNIQUE just skips those rows (same as pre-fix behavior).
    # Migration #325 tightens ``resource_tokens.resource_pk`` to NOT NULL,
    # at which point this branch goes away.
    try:
        event = ResourceEvent(
            resource_id=resource_id,
            resource_pk=token_record.resource_pk,
            op=request.op,
            doc_id=request.doc_id,
            version=request.version,
            payload=request.payload,
            idempotency_key=request.idempotency_key,
            event_metadata=request.event_metadata,
            importance=request.importance if request.importance is not None else 0.6,  # Issue #262
        )

        db.add(event)
        await db.commit()
        await db.refresh(event)

        logger.info(
            "resource_event_created",
            event_id=event.id,
            resource_id=resource_id,
            op=request.op,
            doc_id=request.doc_id,
        )

        # 4. Schedule indexer run (find all contexts using this resource)
        await _schedule_indexer_for_resource(db, context.workspace_id, resource_id)

        # 5. Log usage statistics (Issue #242)
        from utils.usage_logger import log_usage

        await log_usage(
            db=db,
            user_id=token_record.created_by or "system",
            endpoint=f"/api/v1/resources/{resource_id}/events",
            method="POST",
            status_code=201,
            response_time_ms=None,
            context_id=str(context.id) if context.id is not None else None,
            workspace_id=str(context.workspace_id),
        )

        return ResourceEventResponse(
            status="success",
            event_id=event.id,
            queued=True,
            estimated_indexing_time_seconds=None,  # Removed misleading estimate
        )

    except IntegrityError as e:
        await db.rollback()
        constraint = integrity_error_constraint_name(e)

        if constraint == RESOURCE_EVENTS_UPSERT_UNIQUE:
            raise ConflictError(
                f"Version {request.version} already exists for document {request.doc_id}"
            ) from e

        if constraint == RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE:
            result = await db.execute(
                select(ResourceEvent).where(
                    ResourceEvent.idempotency_key == request.idempotency_key
                )
            )
            existing_event = result.scalar_one_or_none()

            if existing_event:
                logger.info(
                    "idempotent_request_detected",
                    idempotency_key=request.idempotency_key,
                    existing_event_id=existing_event.id,
                )
                return ResourceEventResponse(
                    status="success",
                    event_id=existing_event.id,
                    queued=False,
                    estimated_indexing_time_seconds=0,
                )

        # Unknown constraint. Raise HTTPException(500) rather than a bare
        # re-raise of IntegrityError: the app-wide
        # ``@app.exception_handler(SQLAlchemyError)`` in ``api/main.py``
        # converts every SQLAlchemyError into a 503 "database_unavailable",
        # which would be wrong here (the DB is fine; the write was rejected).
        # The structured log above carries the constraint name for alerting.
        logger.error(
            "resource_event_integrity_error_unhandled",
            constraint=constraint,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create event due to database constraint",
        ) from e


@router.post(
    "/{resource_id}/events/batch",
    response_model=ResourceEventBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_batch(
    resource_id: str,
    request: ResourceEventBatchRequest,
    auth: tuple[ResourceToken, int, Any] = Depends(verify_resource_token),
    db: AsyncSession = Depends(get_db),
):
    """Ingest multiple resource events in a single request.

    Issue #238: Batch ingestion for efficiency (up to 100 events).

    Security:
        - Same as single event ingestion
        - Quota checked for entire batch

    Request:
        POST /api/v1/resources/{resource_id}/events/batch
        Headers:
            X-Resource-API-Key: kagura_resource_...
        Body:
            {
                "events": [
                    {"op": "upsert", "doc_id": "PROD-1", "version": 1, "payload": {...}},
                    {"op": "delete", "doc_id": "PROD-999", "version": 5}
                ]
            }

    Response:
        {
            "status": "success",
            "created_count": 2,
            "failed_count": 0,
            "event_ids": [12345, 12346],
            "errors": []
        }

    Errors:
        - 401: Invalid/revoked token
        - 403: Cross-tenant ingest blocked (Issue #322)
        - 404: Resource not bound to an active Context
        - 422: Validation error (max 100 events)
        - 429: Quota exceeded
    """
    # Context is resolved and workspace-verified in the dependency — always non-None here.
    token_record, quota_per_hour, context = auth

    logger.info(
        "resource_batch_ingest_started",
        resource_id=resource_id,
        batch_size=len(request.events),
    )

    # 1. Validate batch size (already enforced by Pydantic, but double-check)
    if len(request.events) > resource_ingest_service.MAX_BATCH_SIZE:
        raise ValidationError(
            f"Batch size exceeds maximum ({resource_ingest_service.MAX_BATCH_SIZE} events)"
        )

    # 2. Check quota for entire batch (workspace-scoped counter shared with MCP).
    #    The token dependency already resolved the effective per-hour quota;
    #    RateLimitError propagates as the existing 429 contract.
    await check_event_quota(
        resource_id, context.workspace_id, quota_per_hour, count=len(request.events)
    )

    # 3. Domain validation + persistence via the shared service (Issue #1255).
    #    Inputs are Pydantic-validated, so the validation pass only adds the
    #    byte-accurate payload-size check; the token carries the authoritative
    #    resource_pk, so no slug re-resolution is needed on this surface.
    valid_events, validation_errors = resource_ingest_service.validate_events(
        [event_req.model_dump() for event_req in request.events]
    )
    result = await resource_ingest_service.persist_events(
        db,
        resource_id=resource_id,
        resource_pk=token_record.resource_pk,
        events=valid_events,
    )

    # Historic REST ordering: item errors sorted by event index (validation
    # and persistence failures interleaved in a single sequential loop).
    errors = [
        _format_batch_item_error(err)
        for err in sorted([*validation_errors, *result.errors], key=lambda e: e.index)
    ]
    created_ids = result.created_ids

    # 4. Commit + post-commit indexer boundary (shared service). The scheduler
    #    is injected so the service never imports from this adapter layer.
    await resource_ingest_service.finalize_batch(
        db,
        workspace_id=context.workspace_id,
        resource_id=resource_id,
        created_ids=created_ids,
        schedule_indexer=_schedule_indexer_for_resource,
    )

    if created_ids:
        # 5. Log usage statistics (Issue #242)
        from utils.usage_logger import log_usage

        await log_usage(
            db=db,
            user_id=token_record.created_by or "system",
            endpoint=f"/api/v1/resources/{resource_id}/events/batch",
            method="POST",
            status_code=201,
            response_time_ms=None,
            context_id=str(context.id) if context.id is not None else None,
            workspace_id=str(context.workspace_id),
        )

    logger.info(
        "resource_batch_ingest_completed",
        resource_id=resource_id,
        created_count=len(created_ids),
        failed_count=len(errors),
    )

    return ResourceEventBatchResponse(
        status="success",
        created_count=len(created_ids),
        failed_count=len(errors),
        event_ids=created_ids,
        errors=errors,
    )


# ============================================================================
# Helper Functions
# ============================================================================


async def _schedule_indexer_for_resource(
    db: AsyncSession,
    workspace_id: UUID,
    resource_id: str,
) -> None:
    """Schedule indexer runs for all contexts using this resource.

    Resolves the workspace-scoped ``resource_pk`` and writes it onto every
    IndexerState row, satisfying the Phase 2 writer contract enforced at
    ``models/resource.py:_enforce_resource_pk_invariant`` (#323) and avoiding
    the slug-only filter that is a CWE-639 cross-tenant leak vector when slug
    reuse is possible (recall PR #347 iter 1).

    Args:
        db: Database session
        workspace_id: Authoritative workspace scope for the slug lookup
        resource_id: Resource identifier (slug)
    """
    from datetime import timedelta

    from models.auth import Context
    from models.resource import IndexerState

    resource_pk = await resolve_resource_pk(db, workspace_id, resource_id)
    if resource_pk is None:
        # Fail-safe: missing Resource row indicates an orphan or cross-workspace
        # probe; do not schedule against a resource we cannot bind.
        logger.debug(
            "schedule_indexer_skip_unknown_resource",
            resource_id=resource_id,
            workspace_id=str(workspace_id),
        )
        return

    # Find all public contexts using this resource. Scope to ``workspace_id``
    # so a slug reused in another workspace cannot pull foreign Contexts into
    # the iteration — every IndexerState row written below carries the caller's
    # ``resource_pk`` and a ``context_id`` from the same workspace, keeping
    # the (resource_pk, context_id) pairing consistent (Copilot review on PR
    # for #456 + gate2 CSO note).
    result = await db.execute(
        select(Context).where(
            Context.workspace_id == workspace_id,
            Context.resource_id == resource_id,
            Context.is_public.is_(True),
            Context.deleted_at.is_(None),
        )
    )
    contexts = list(result.scalars().all())

    if not contexts:
        logger.debug("no_contexts_for_resource", resource_id=resource_id)
        return

    # Schedule indexer for each context
    for context in contexts:
        # Get or create indexer state. Prefer rows with resource_pk populated;
        # fall back to legacy ``resource_pk IS NULL`` rows scoped by slug +
        # context_id so Phase 1 → Phase 2 in-flight rows still match.
        state_result = await db.execute(
            select(IndexerState)
            .where(
                IndexerState.context_id == context.id,
                or_(
                    IndexerState.resource_pk == resource_pk,
                    (IndexerState.resource_pk.is_(None))
                    & (IndexerState.resource_id == resource_id),
                ),
            )
            .order_by(
                IndexerState.resource_pk.is_(None).asc(),
                IndexerState.id.desc(),
            )
            .limit(1)
        )
        state = state_result.scalar_one_or_none()

        if not state:
            # Create new state
            state = IndexerState(
                resource_pk=resource_pk,
                resource_id=resource_id,
                context_id=context.id,
                last_offset=0,
                job_status="queued",
                next_run_at=utcnow() + timedelta(minutes=1),  # Run in 1 minute
            )
            db.add(state)
        elif state.job_status == "idle":
            # Update to queued
            state.job_status = "queued"
            state.next_run_at = utcnow() + timedelta(minutes=1)

        await db.flush()

    logger.info(
        "indexer_scheduled_for_contexts",
        resource_id=resource_id,
        context_count=len(contexts),
    )
