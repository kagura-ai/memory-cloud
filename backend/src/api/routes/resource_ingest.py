"""Resource Ingest API routes.

Issue #238: Resource-driven incremental indexing for Public Contexts.

Provides endpoints for external systems (EC inventory, etc.) to push events.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.resource_tokens import ResourceTokenManager
from db.base import get_db
from models.auth import Context
from models.resource import ResourceEvent, ResourceToken
from models.schemas import (
    ResourceEventBatchRequest,
    ResourceEventBatchResponse,
    ResourceEventRequest,
    ResourceEventResponse,
)
from services.permission_service import PermissionService
from utils.datetime import utcnow
from utils.exceptions import ConflictError, RateLimitError, RedisError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/resources", tags=["resource-ingest"])

# Maximum payload size (100KB)
MAX_PAYLOAD_SIZE_BYTES = 100_000


# ============================================================================
# Dependency: Resource Token Authentication
# ============================================================================


async def verify_resource_token(
    resource_id: str,
    request: Request,
    x_resource_api_key: str | None = Header(None, alias="X-Resource-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> tuple[ResourceToken, int, Context]:
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
        logger.warning(
            "resource_id_unbound_on_ingest",
            resource_id=resource_id,
            token_id=token_record.id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource is not bound to an active context",
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
    except HTTPException as auth_error:
        # `reason` captures the underlying cause (workspace deleted /
        # non-member / role-too-low) for forensics without re-leaking the
        # detail to the caller.
        logger.warning(
            "cross_tenant_ingest_attempt",
            resource_id=context.resource_id,
            token_id=token_record.id,
            target_workspace_id=str(context.workspace_id),
            token_creator=token_record.created_by,
            client_ip=request.client.host if request.client else None,
            reason=auth_error.detail,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Resource ingest denied: the token's creator does not have "
                "active access to the workspace that owns this resource."
            ),
        ) from auth_error


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
    auth: tuple[ResourceToken, int, Context] = Depends(verify_resource_token),
    db: AsyncSession = Depends(get_db),
):
    """Ingest a single resource event (upsert or delete).

    Issue #238: Append-only event log for incremental indexing.

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

    logger.info(
        "resource_event_ingest_started",
        resource_id=resource_id,
        op=request.op,
        doc_id=request.doc_id,
        version=request.version,
        has_payload=request.payload is not None,
    )

    # 1. Check quota (events per hour)
    await _check_event_quota(resource_id, token_record.id, quota_per_hour)

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
    try:
        event = ResourceEvent(
            resource_id=resource_id,
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
        await _schedule_indexer_for_resource(db, resource_id)

        # 5. Log usage statistics (Issue #242)
        from utils.usage_logger import log_usage

        await log_usage(
            db=db,
            user_id=token_record.created_by or "system",
            endpoint=f"/api/v1/resources/{resource_id}/events",
            method="POST",
            status_code=201,
            response_time_ms=None,
            context_id=str(context.id),
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
        error_msg = str(e)

        # Handle duplicate version error
        if "unique_resource_doc_version" in error_msg:
            raise ConflictError(
                f"Version {request.version} already exists for document {request.doc_id}"
            ) from e

        # Handle duplicate idempotency key
        if "unique_idempotency_key" in error_msg:
            # Find existing event with this idempotency key
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
                # Return existing event (202 Accepted - idempotent success)
                return ResourceEventResponse(
                    status="success",
                    event_id=existing_event.id,
                    queued=False,  # Already processed
                    estimated_indexing_time_seconds=0,
                )

        # Unknown integrity error
        logger.error("resource_event_integrity_error", error=error_msg)
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
    auth: tuple[ResourceToken, int, Context] = Depends(verify_resource_token),
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
    if len(request.events) > 100:
        raise ValidationError("Batch size exceeds maximum (100 events)")

    # 2. Check quota for entire batch
    await _check_event_quota(
        resource_id, token_record.id, quota_per_hour, count=len(request.events)
    )

    # 3. Process events
    created_ids: list[int] = []
    errors: list[dict] = []

    for idx, event_req in enumerate(request.events):
        try:
            # Validate payload size
            if event_req.payload:
                import json

                payload_size = len(json.dumps(event_req.payload))
                if payload_size > MAX_PAYLOAD_SIZE_BYTES:
                    errors.append(
                        {
                            "index": idx,
                            "doc_id": event_req.doc_id,
                            "error": f"Payload too large: {payload_size} bytes",
                        }
                    )
                    continue

            # Create event
            event = ResourceEvent(
                resource_id=resource_id,
                op=event_req.op,
                doc_id=event_req.doc_id,
                version=event_req.version,
                payload=event_req.payload,
                idempotency_key=event_req.idempotency_key,
                event_metadata=event_req.event_metadata,
                importance=event_req.importance
                if event_req.importance is not None
                else 0.6,  # Issue #262
            )

            db.add(event)
            await db.flush()
            created_ids.append(event.id)

        except IntegrityError as e:
            error_msg = str(e)

            # Duplicate version (skip silently)
            if "unique_resource_doc_version" in error_msg:
                logger.debug(
                    "duplicate_version_skipped",
                    doc_id=event_req.doc_id,
                    version=event_req.version,
                )
                errors.append(
                    {
                        "index": idx,
                        "doc_id": event_req.doc_id,
                        "error": f"Duplicate version {event_req.version}",
                    }
                )

            # Duplicate idempotency key (skip silently)
            elif "unique_idempotency_key" in error_msg:
                logger.debug("duplicate_idempotency_key_skipped", key=event_req.idempotency_key)
                errors.append(
                    {
                        "index": idx,
                        "doc_id": event_req.doc_id,
                        "error": "Duplicate idempotency key",
                    }
                )

            else:
                # Unknown error
                logger.error("batch_event_failed", index=idx, error=error_msg)
                errors.append(
                    {
                        "index": idx,
                        "doc_id": event_req.doc_id,
                        "error": "Database constraint violation",
                    }
                )

        except Exception as e:
            logger.error("batch_event_unexpected_error", index=idx, error=str(e))
            errors.append(
                {
                    "index": idx,
                    "doc_id": event_req.doc_id,
                    "error": str(e),
                }
            )

    # 4. Commit all successfully created events
    # NOTE: Batch ingest uses partial-success model (some events can fail while others succeed)
    # This is intentional for resilience. If atomic behavior is needed, use individual requests.
    await db.commit()

    # 5. Schedule indexer run
    if created_ids:
        await _schedule_indexer_for_resource(db, resource_id)

        # 6. Log usage statistics (Issue #242)
        from utils.usage_logger import log_usage

        await log_usage(
            db=db,
            user_id=token_record.created_by or "system",
            endpoint=f"/api/v1/resources/{resource_id}/events/batch",
            method="POST",
            status_code=201,
            response_time_ms=None,
            context_id=str(context.id),
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


async def _check_event_quota(
    resource_id: str,
    token_id: int,
    quota_per_hour: int,
    count: int = 1,
) -> None:
    """Check event ingestion quota (per token per hour).

    Uses Redis counter with 1-hour TTL.

    Args:
        resource_id: Resource identifier
        token_id: Token ID
        quota_per_hour: Maximum events allowed per hour
        count: Number of events to check (default: 1)

    Raises:
        RateLimitError: If quota exceeded (429)
    """
    from db.redis import get_cache, incrby_counter

    redis_key = f"resource:events:{resource_id}:{token_id}:hour"

    try:
        current_count_str = await get_cache(redis_key)
        current_count = int(current_count_str) if current_count_str else 0

        if current_count + count > quota_per_hour:
            logger.warning(
                "resource_event_quota_exceeded",
                resource_id=resource_id,
                token_id=token_id,
                current=current_count,
                quota=quota_per_hour,
            )
            raise RateLimitError(
                message=f"Event quota exceeded: {current_count}/{quota_per_hour} events per hour",
                retry_after=3600,  # Retry after 1 hour
            )

        new_count = await incrby_counter(redis_key, count, ttl=3600)

        logger.debug(
            "resource_event_quota_checked",
            resource_id=resource_id,
            previous=current_count,
            reserved=new_count,
            quota=quota_per_hour,
        )

    except RateLimitError:
        raise
    except RedisError as e:
        # Fail-open per SECURITY.md "Rate Limiting": Redis outage must not block ingest.
        # Narrow to RedisError so programming bugs (ValueError from parse, etc.) surface.
        logger.error("redis_quota_check_failed", error=str(e))
        return


async def _schedule_indexer_for_resource(db: AsyncSession, resource_id: str) -> None:
    """Schedule indexer runs for all contexts using this resource.

    Args:
        db: Database session
        resource_id: Resource identifier
    """
    from datetime import timedelta

    from models.auth import Context
    from models.resource import IndexerState

    # Find all public contexts using this resource
    result = await db.execute(
        select(Context).where(
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
        # Get or create indexer state
        state_result = await db.execute(
            select(IndexerState).where(
                IndexerState.resource_id == resource_id,
                IndexerState.context_id == context.id,
            )
        )
        state = state_result.scalar_one_or_none()

        if not state:
            # Create new state
            state = IndexerState(
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
