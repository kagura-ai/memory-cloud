"""Provider-neutral paid-rights snapshot contract for the billing publisher."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from models.api_base import TZAwareBaseModel

Reference = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
ServiceId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]


class EntitlementWindow(TZAwareBaseModel):
    """A verified finite, nonempty paid interval [valid_from, valid_until)."""

    model_config = ConfigDict(extra="forbid")
    valid_from: datetime
    valid_until: datetime
    evidence_ref: Reference

    @field_validator("valid_from", "valid_until")
    @classmethod
    def qualified_utc(cls, value: datetime) -> datetime:
        """Require an explicit timezone and normalize equivalent offsets for retries."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("An explicit timezone is required")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        """Refuse zero-length and reversed paid intervals."""
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        return self


class ServiceEntitlement(EntitlementWindow):
    """Finite entitlement for one publisher-defined service identifier."""

    service_id: ServiceId


class IntegrationEntitlementPush(TZAwareBaseModel):
    """Complete ordered workspace state, not an unverified payment event."""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(gt=0, le=9_223_372_036_854_775_807, strict=True)
    event_id: Reference
    source_ref: Reference
    max_access: EntitlementWindow | None
    services: list[ServiceEntitlement] = Field(max_length=32)

    @model_validator(mode="after")
    def unique_services(self) -> Self:
        """One workspace snapshot cannot contain ambiguous duplicate service grants."""
        if len({item.service_id for item in self.services}) != len(self.services):
            raise ValueError("Duplicate service identifiers")
        return self


class IntegrationEntitlementView(TZAwareBaseModel):
    """Current persisted state for trusted reconciliation (revision zero if absent)."""

    workspace_id: UUID
    plan_name: str
    revision: int
    event_id: str | None
    source: Literal["external_billing"] | None
    source_ref: str | None
    max_access: EntitlementWindow | None
    services: list[ServiceEntitlement]
    updated_at: datetime | None


class IntegrationEntitlementResult(IntegrationEntitlementView):
    """Applied state, or the current newer state on a harmless delivery retry."""

    applied: bool
    outcome: Literal["applied", "duplicate", "stale"]
