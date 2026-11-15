"""Metadata-only, bounded observation contracts for current KMC members."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from models.api_base import TZAwareBaseModel
from models.public_usage_budget import PublicUsageBudgetStatus
from models.sync_activity import SyncActivityAction
from models.sync_feed_schemas import StrictBody


class ManualRequestBody(StrictBody):
    """The human's immutable request identity; no actor claims are accepted."""

    operation_id: UUID


class ManualRequestView(TZAwareBaseModel):
    request_id: UUID
    operation_id: UUID
    expires_at: datetime
    generation: int


class NotificationSignal(TZAwareBaseModel):
    thresholds: list[Literal[80, 90, 100]]
    reached: Literal[80, 90, 100] | None
    dedupe_key: str | None
    delivery_owner: Literal["public"]


class ActivityOverview(TZAwareBaseModel):
    contract_version: Literal["sync-activity-v1"]
    installation_id: UUID
    workspace_id: UUID
    state: str
    generation: int
    management_mode: Literal["feed", "stopping", "manual"] | None
    management_epoch: int | None
    stop_watermark: int | None
    feed_count: int
    record_count: int
    allowance: PublicUsageBudgetStatus
    notification: NotificationSignal
    as_of: datetime


class SearchObservation(TZAwareBaseModel):
    state: Literal[
        "not_evaluated", "not_committed", "superseded", "unchanged", "pending", "failed", "complete"
    ]
    reason: Literal["index_not_current", "index_pending"] | None = None


class RunObservation(TZAwareBaseModel):
    run_id: UUID
    feed_id: UUID
    operation_id: UUID
    state: Literal["uploading", "prepared", "committed", "aborted", "expired"]
    changes: int | None
    requires_approval: bool
    expires_at: datetime
    prepared_until: datetime | None
    origin: Literal["manual", "unattributed"]
    manual_request_id: UUID | None
    declared_records: int | None
    declared_chunks: int | None
    accepted_at: datetime
    committed_at: datetime | None
    search: SearchObservation


class RunDetail(RunObservation):
    received_chunks: int
    received_records: int
    as_of: datetime


class RunPage(TZAwareBaseModel):
    items: list[RunObservation] = Field(max_length=100)
    next_cursor: UUID | None
    as_of: datetime


class AuditActorView(TZAwareBaseModel):
    kind: Literal["human", "service", "operator", "system"]
    client_id: str | None
    issuer: str | None
    subject_ref: str


class ActivityEvent(TZAwareBaseModel):
    correlation_id: UUID | None
    id: int
    action: SyncActivityAction
    at: datetime
    actor: AuditActorView
    workspace_id: UUID | None
    service_id: str | None
    installation_id: UUID | None
    resource_pk: UUID | None
    context_id: UUID | None
    endpoint_id: UUID | None
    authorization_id: UUID | None
    feed_id: UUID | None
    run_id: UUID | None
    operation_id: UUID | None
    manual_request_id: UUID | None
    key_id: UUID | None
    generation: int | None
    reason: Literal["unauthorized", "forbidden", "not_found"] | None
    requested_by_ref: str | None
    mode: Literal["feed", "stopping", "manual"] | None
    management_epoch: int | None
    stop_watermark: int | None
    drain_generation: int | None


class EventPage(TZAwareBaseModel):
    items: list[ActivityEvent] = Field(max_length=100)
    next_before_id: int | None
    retention_days: Literal[90]
    as_of: datetime
