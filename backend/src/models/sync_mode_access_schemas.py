"""Readonly lifecycle capability metadata, separate from canonical data rights."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from models.sync_feed_schemas import StrictBody

Positive = Annotated[StrictInt, Field(ge=1, le=2**53 - 1)]
Counter = Annotated[StrictInt, Field(ge=0, le=2**53 - 1)]


class SyncModeTarget(StrictBody):
    """The retained physical binding, without a public URL or credential."""

    workspace_id: UUID
    installation_id: UUID
    resource_pk: UUID
    context_id: UUID
    endpoint_id: UUID
    generation: Positive
    target_available: bool


class SyncModeStatus(StrictBody):
    """Current management facts are observations, never mutation receipts."""

    mode: Literal["feed", "stopping", "manual"]
    epoch: Positive
    generation: Positive
    stop_event_id: Counter
    reason: Annotated[str, Field(min_length=1, max_length=128)] | None


class SyncModePermissions(StrictBody):
    """Only this metadata and safe stop/finalize, not data writes or resume."""

    read: Literal[True]
    stop: bool
    finalize: bool


class SyncModeAccess(StrictBody):
    """Current human lifecycle access under the existing capacity-safe boundary."""

    contract_version: Literal["sync-mode-access-v1"]
    subject: Annotated[str, Field(min_length=1, max_length=255)]
    target: SyncModeTarget
    management: SyncModeStatus
    permissions: SyncModePermissions

    @model_validator(mode="after")
    def consistent_generation(self) -> Self:
        """The target and management snapshot must describe the same generation."""
        if self.target.generation != self.management.generation:
            raise ValueError("Lifecycle access generation changed")
        return self
