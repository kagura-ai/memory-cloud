"""Atomic ordered workspace billing contract, independent of payment providers."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from config.billing_addons import ADDON_COLUMNS, ADDON_MAX_BONUS, ADDON_MAX_BY_KEY
from models.integration_entitlement_schemas import (
    IntegrationEntitlementPush,
    IntegrationEntitlementResult,
    IntegrationEntitlementView,
)

Bonus = Annotated[int, Field(strict=True, ge=0, le=ADDON_MAX_BONUS)]


class BillingSnapshotPlan(BaseModel):
    """Complete plan and all canonical addon dimensions, without partial updates."""

    model_config = ConfigDict(extra="forbid")
    plan_name: Literal["free", "basic", "pro", "promax"]
    addons: dict[str, Bonus]

    @model_validator(mode="after")
    def complete_addons(self) -> Self:
        if set(self.addons) != set(ADDON_COLUMNS):
            raise ValueError("The complete canonical addon set is required")
        for key, maximum in ADDON_MAX_BY_KEY.items():
            if self.addons[key] > maximum:
                raise ValueError("Addon exceeds its domain maximum")
        return self


class WorkspaceBillingPush(IntegrationEntitlementPush):
    """One revision controls plan, quotas and every finite service interval."""

    contract_version: Literal["workspace-billing-snapshot-v1"]
    plan: BillingSnapshotPlan


class WorkspaceBillingView(IntegrationEntitlementView):
    contract_version: Literal["workspace-billing-snapshot-v1"] = "workspace-billing-snapshot-v1"
    plan: BillingSnapshotPlan
    ordered_billing: bool
    entitlement_source: Literal["admin_grant", "external_billing"]


class WorkspaceBillingResult(IntegrationEntitlementResult):
    contract_version: Literal["workspace-billing-snapshot-v1"] = "workspace-billing-snapshot-v1"
    plan: BillingSnapshotPlan
    ordered_billing: bool
    entitlement_source: Literal["admin_grant", "external_billing"]
