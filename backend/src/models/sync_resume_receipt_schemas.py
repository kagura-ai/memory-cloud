"""Strict historical outcome metadata, never renewed publication authority."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, StrictInt, model_validator

from models.api_base import TZAwareBaseModel
from models.sync_feed_schemas import Hash, StrictBody

Subject = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$")]
Positive = Annotated[StrictInt, Field(ge=1, le=2**53 - 1)]
Counter = Annotated[StrictInt, Field(ge=0, le=2**53 - 1)]


class SyncResumeReceiptCommand(StrictBody):
    """Name the original immutable operation, body and actor without impersonation."""

    operation_id: UUID
    preview_id: UUID
    diff_hash: Hash
    original_subject: Subject


class SyncResumeModeReceipt(StrictBody):
    """Only the saved resume outcome; this is not current management state."""

    mode: Literal["feed"]
    epoch: Positive
    generation: Positive
    stop_event_id: Counter
    reason: None


class SyncResumeReceiptView(TZAwareBaseModel):
    """An authoritative observation under the current Editor's transaction fence."""

    # Preserve declared response types despite the shared UTC wrap serializer's
    # Any return type. That serializer changes UTC spelling, not field types.
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_mode_override="validation")

    contract_version: Literal["sync-resume-receipt-v1"]
    workspace_id: UUID
    installation_id: UUID
    operation_id: UUID
    preview_id: UUID
    diff_hash: Hash
    original_subject: Subject
    result: Literal["found", "not_found", "mismatch"]
    receipt: SyncResumeModeReceipt | None
    as_of: datetime

    @model_validator(mode="after")
    def consistent_receipt(self) -> Self:
        """Missing or unrelated operations must not expose a saved mode receipt."""
        if (self.result == "found") != (self.receipt is not None):
            raise ValueError("Receipt does not match observation result")
        return self
