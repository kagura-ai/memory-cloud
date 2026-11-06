"""Read-only observability API for a self-hosted dashboard (#2679).

Every route here authenticates with ``get_observation_principal`` and nothing
else: a scoped observation credential (``kagura_obs_…``), never a session or
an owner key, so no browser has to hold an owner credential. The routes are
internal to the deployment: ``OBSERVABILITY_API_ENABLED`` (default off) makes
every route answer 404 before any header is read, and
``OBSERVABILITY_API_ALLOWED_CIDRS`` limits the client addresses.

Scope comes from the credential row only (one workspace, or one resource of
it). Every response carries ``schema_version``, ``deployment_id`` and
``as_of``; a ``schema_version`` other than 1 is a 400
``unsupported_schema_version`` naming the supported versions.

Payload minimisation: no ingestion payload, document id, version, memory id,
memory text, error message or indexer ``metrics`` passthrough leaves here.
``docs/observability-api.md`` documents every route and error body.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.observation_credentials import ObservationScope, get_observation_principal
from config.settings import get_settings
from db.base import get_db
from models.api_base import TZAwareBaseModel
from services.observability_ingestion import INGESTION_PAGE_MAX, read_ingestion_page
from services.observability_scope import (
    OBSERVABILITY_RESOURCES_MAX,
    ScopedResource,
    publication_block,
    resolve_scoped_resource,
    scoped_resources,
)
from services.resource_indexer import load_indexer_state_row, sanitized_indexer_state
from utils.datetime import utcnow
from utils.exceptions import UnsupportedSchemaVersionError

router = APIRouter(prefix="/observability", tags=["observability"])

SUPPORTED_SCHEMA_VERSIONS = [1]
EVENTS_PAGE_MAX = 500
DOCUMENTS_RESOLVE_MAX = 100

Principal = ObservationScope

_SCHEMA_VERSION = Query(1, description="Response schema version; only 1 is served")
_RESOURCE_ID = Query(
    None,
    min_length=1,
    max_length=255,
    description="Resource slug; optional for a resource-scoped credential",
)


def _check_schema_version(requested: int) -> None:
    if requested not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersionError(requested, SUPPORTED_SCHEMA_VERSIONS)


def _deployment_id() -> str:
    return str(getattr(get_settings(), "deployment_id", "default"))


def _usage_retention_days() -> int:
    return int(getattr(get_settings(), "mcp_public_telemetry_retention_days", 90))


def _usage_writer_enabled() -> bool:
    return bool(getattr(get_settings(), "mcp_public_telemetry_enabled", False))


# ============================================================================
# Response models (schema version 1)
# ============================================================================


class _V1(TZAwareBaseModel):
    """Schema v1 models refuse unknown fields (the fixtures are checked against them)."""

    model_config = ConfigDict(extra="forbid")


class _Envelope(_V1):
    schema_version: Literal[1] = 1
    deployment_id: str
    as_of: datetime


class PublicationV1(_V1):
    """The resource's live public MCP endpoint (#2675)."""

    endpoint_id: UUID
    public_id: str
    public_name: str
    published_at: datetime
    updated_at: datetime | None = None
    serving: bool = Field(..., description="Whether the anonymous endpoint answers right now")


class CapabilityScopeV1(_V1):
    kind: Literal["workspace", "resource"]
    workspace_id: UUID
    resource_id: str | None = None


class CapabilityCredentialV1(_V1):
    id: str
    expires_at: datetime


class CapabilityLimitsV1(_V1):
    events_page_max: int
    ingestion_page_max: int
    documents_resolve_max: int
    resources_max: int


class CapabilityRetentionV1(_V1):
    usage_events_days: int
    ingestion_events: Literal["resource lifetime"] = "resource lifetime"


class CapabilitiesV1(_Envelope):
    """``GET /observability/capabilities``."""

    schema_versions: dict[str, list[int]]
    scope: CapabilityScopeV1
    credential: CapabilityCredentialV1
    limits: CapabilityLimitsV1
    retention: CapabilityRetentionV1
    ingestion_seal_seconds: int
    writer_enabled: bool
    server_time: datetime


class ResourceItemV1(_V1):
    resource_pk: UUID
    resource_id: str
    name: str | None = None
    context_id: UUID | None = None
    created_at: datetime
    publication: PublicationV1 | None = None


class ResourcesV1(_Envelope):
    """``GET /observability/resources`` (at most 500, no pagination)."""

    resources: list[ResourceItemV1]
    truncated: bool = Field(False, description="More resources exist than were returned")


class IndexerLastRunV1(_V1):
    applied_upserts: int
    applied_deletes: int
    errors: int
    failed_events: int = Field(..., description="Count of events recorded as failed (capped)")
    skipped_reason: str | None = None


class IndexerSnapshotV1(_V1):
    state: Literal["never_run", "idle", "queued", "running", "error"]
    last_run_at: datetime | None = None
    seconds_since_last_run: float | None = None
    next_run_at: datetime | None = None
    last_offset: int | None = None
    last_run: IndexerLastRunV1 | None = None


class SnapshotItemV1(_V1):
    resource_pk: UUID
    resource_id: str
    context_id: UUID | None = None
    indexer: IndexerSnapshotV1
    publication: PublicationV1 | None = None


FIELD_NOTES = {
    "seconds_since_last_run": "age of the last indexer run; not ingest lag",
    "last_offset": "the last processed ingestion event id; not a backlog count",
}


class SnapshotsV1(_Envelope):
    """``GET /observability/snapshots``."""

    snapshots: list[SnapshotItemV1]
    truncated: bool = False
    field_notes: dict[str, str] = Field(default_factory=lambda: dict(FIELD_NOTES))


class IngestionProcessingV1(_V1):
    status: Literal["processed", "pending", "unknown"]
    failed: bool


class IngestionEventV1(_V1):
    event_id: str
    received_at: datetime
    op: Literal["upsert", "delete"]
    doc_key: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    processing: IngestionProcessingV1


class IngestionCoverageV1(_V1):
    sealed_before: datetime
    seal_seconds: int
    retention: Literal["resource lifetime"] = "resource lifetime"
    oldest_event_id: str | None = None


class IngestionEventsPageV1(_Envelope):
    """``GET /observability/ingestion-events``."""

    resource_id: str
    events: list[IngestionEventV1]
    next_cursor: str
    has_more: bool
    coverage: IngestionCoverageV1


# ============================================================================
# Helpers
# ============================================================================


async def _publication(db: AsyncSession, resource: ScopedResource) -> PublicationV1 | None:
    block = await publication_block(db, resource)
    return PublicationV1(**block) if block is not None else None


async def _snapshot(db: AsyncSession, resource: ScopedResource) -> SnapshotItemV1:
    state_row = (
        await load_indexer_state_row(
            db, resource.context_id, resource.resource_pk, resource.resource_id
        )
        if resource.context_id is not None
        else None
    )
    if state_row is None or state_row.last_run_at is None:
        indexer = IndexerSnapshotV1(
            state="never_run",
            next_run_at=state_row.next_run_at if state_row is not None else None,
        )
    else:
        state = sanitized_indexer_state(state_row)
        indexer = IndexerSnapshotV1(
            state="error" if state_row.job_status == "failed" else state_row.job_status,  # type: ignore[arg-type]
            last_run_at=state_row.last_run_at,
            seconds_since_last_run=state["lag_seconds"],
            next_run_at=state_row.next_run_at,
            last_offset=state_row.last_offset,
            last_run=IndexerLastRunV1(**state["metrics"]),
        )
    return SnapshotItemV1(
        resource_pk=resource.resource_pk,
        resource_id=resource.resource_id,
        context_id=resource.context_id,
        indexer=indexer,
        publication=await _publication(db, resource),
    )


# ============================================================================
# Routes
# ============================================================================


@router.get("/capabilities", response_model=CapabilitiesV1)
async def get_capabilities(
    scope: Principal = Depends(get_observation_principal),
    schema_version: int = _SCHEMA_VERSION,
    db: AsyncSession = Depends(get_db),
) -> CapabilitiesV1:
    """What this credential may read, the served schema versions and the limits."""
    _check_schema_version(schema_version)
    resource_slug = None
    if scope.resource_pk is not None:
        bound, _ = await scoped_resources(db, scope, limit=1)
        resource_slug = bound[0].resource_id if bound else None
    now = utcnow()
    return CapabilitiesV1(
        deployment_id=_deployment_id(),
        as_of=now,
        schema_versions={
            "resources": [1],
            "snapshots": [1],
            "events": [1],
            "ingestion_events": [1],
            "documents": [1],
        },
        scope=CapabilityScopeV1(
            kind=scope.kind, workspace_id=scope.workspace_id, resource_id=resource_slug
        ),
        credential=CapabilityCredentialV1(id=scope.public_id, expires_at=scope.expires_at),
        limits=CapabilityLimitsV1(
            events_page_max=EVENTS_PAGE_MAX,
            ingestion_page_max=INGESTION_PAGE_MAX,
            documents_resolve_max=DOCUMENTS_RESOLVE_MAX,
            resources_max=OBSERVABILITY_RESOURCES_MAX,
        ),
        retention=CapabilityRetentionV1(usage_events_days=_usage_retention_days()),
        ingestion_seal_seconds=get_settings().observability_ingestion_seal_seconds,
        writer_enabled=_usage_writer_enabled(),
        server_time=now,
    )


@router.get("/resources", response_model=ResourcesV1)
async def list_resources(
    scope: Principal = Depends(get_observation_principal),
    schema_version: int = _SCHEMA_VERSION,
    db: AsyncSession = Depends(get_db),
) -> ResourcesV1:
    """The resources in scope with their live publication (at most 500)."""
    _check_schema_version(schema_version)
    resources, truncated = await scoped_resources(db, scope)
    items = [
        ResourceItemV1(
            resource_pk=r.resource_pk,
            resource_id=r.resource_id,
            name=r.name,
            context_id=r.context_id,
            created_at=r.created_at,
            publication=await _publication(db, r),
        )
        for r in resources
    ]
    return ResourcesV1(
        deployment_id=_deployment_id(), as_of=utcnow(), resources=items, truncated=truncated
    )


@router.get("/snapshots", response_model=SnapshotsV1)
async def list_snapshots(
    scope: Principal = Depends(get_observation_principal),
    resource_id: str | None = _RESOURCE_ID,
    schema_version: int = _SCHEMA_VERSION,
    db: AsyncSession = Depends(get_db),
) -> SnapshotsV1:
    """Sanitised indexer state per resource (all in scope, or the one named)."""
    _check_schema_version(schema_version)
    if resource_id is not None:
        resources, truncated = [await resolve_scoped_resource(db, scope, resource_id)], False
    else:
        resources, truncated = await scoped_resources(db, scope)
    return SnapshotsV1(
        deployment_id=_deployment_id(),
        as_of=utcnow(),
        snapshots=[await _snapshot(db, r) for r in resources],
        truncated=truncated,
    )


@router.get("/ingestion-events", response_model=IngestionEventsPageV1)
async def list_ingestion_events(
    scope: Principal = Depends(get_observation_principal),
    resource_id: str | None = _RESOURCE_ID,
    cursor: str | None = Query(None, description="`next_cursor` of the previous page"),
    limit: int = Query(100, ge=1, le=INGESTION_PAGE_MAX),
    schema_version: int = _SCHEMA_VERSION,
    db: AsyncSession = Depends(get_db),
) -> IngestionEventsPageV1:
    """A payload-free, time-sealed page of one resource's ingestion events."""
    _check_schema_version(schema_version)
    resource = await resolve_scoped_resource(db, scope, resource_id)
    page = await read_ingestion_page(db, resource, cursor=cursor, limit=limit)
    return IngestionEventsPageV1(
        deployment_id=_deployment_id(),
        as_of=utcnow(),
        resource_id=resource.resource_id,
        events=[IngestionEventV1(**item) for item in page.events],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        coverage=IngestionCoverageV1(
            sealed_before=page.sealed_before,
            seal_seconds=page.seal_seconds,
            oldest_event_id=str(page.oldest_event_id) if page.oldest_event_id else None,
        ),
    )
