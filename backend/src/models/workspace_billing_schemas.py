"""Atomic ordered workspace billing contract, independent of payment providers."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from config.billing_addons import ADDON_MAX_BONUS, ADDON_MAX_BY_KEY
from models.integration_entitlement_schemas import (
    IntegrationEntitlementPush,
    IntegrationEntitlementResult,
    IntegrationEntitlementView,
)

Bonus = Annotated[int, Field(strict=True, ge=0, le=ADDON_MAX_BONUS)]
# The protocol's keys are frozen independently of future storage dimensions.
BILLING_V1_ADDON_KEYS = (
    "memory",
    "mcp_quota",
    "rest_quota",
    "public_quota",
    "member",
    "context",
    "analysis",
    "storage_mb",
    "sleep_contexts",
    "connector",
    "memories_per_day",
    "history_retention_days",
)


class BillingSnapshotPlan(BaseModel):
    """Complete plan and all canonical addon dimensions, without partial updates."""

    model_config = ConfigDict(extra="forbid")
    plan_name: Literal["free", "basic", "pro", "promax"]
    addons: dict[str, Bonus]

    @model_validator(mode="after")
    def complete_addons(self) -> Self:
        if set(self.addons) != set(BILLING_V1_ADDON_KEYS):
            raise ValueError("The complete canonical addon set is required")
        for key, maximum in ADDON_MAX_BY_KEY.items():
            if key in self.addons and self.addons[key] > maximum:
                raise ValueError("Addon exceeds its domain maximum")
        return self


class WorkspaceBillingPush(IntegrationEntitlementPush):
    """One revision controls plan, quotas and every finite service interval."""

    contract_version: Literal["workspace-billing-snapshot-v1"]
    plan: BillingSnapshotPlan | None


class BillingSnapshotPlanView(BaseModel):
    """Report stored state without imposing the versioned write vocabulary/bounds."""

    plan_name: str
    addons: dict[str, int]


class WorkspaceBillingView(IntegrationEntitlementView):
    contract_version: Literal["workspace-billing-snapshot-v1"] = "workspace-billing-snapshot-v1"
    plan: BillingSnapshotPlanView
    ordered_billing: bool
    entitlement_source: str


class WorkspaceBillingResult(IntegrationEntitlementResult):
    contract_version: Literal["workspace-billing-snapshot-v1"] = "workspace-billing-snapshot-v1"
    plan: BillingSnapshotPlanView
    ordered_billing: bool
    entitlement_source: str
