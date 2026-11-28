"""Non-authorizing, versioned synchronization configuration (#2811)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class SyncConfigurationV1(BaseModel):
    """Compiled wire profile and enabled capability subsets, never tenant authority."""

    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal["kmc.sync.v1"]
    # The version a new installation gets: 2 exactly while ``sync.schema.v2`` is advertised.
    schema_version: StrictInt = Field(ge=1, le=2)
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capabilities: list[str]
    human_scopes: list[str]
    service_scope: Literal["resource.sync"]
    assertion_audience: Literal["issuer"]
