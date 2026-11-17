"""Strict v1 add-on setup context; no price, review result or target overrides."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from models.api_base import TZAwareBaseModel


class SyncSetupHandoffCreate(TZAwareBaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: UUID
    application_revision: int = Field(strict=True, ge=1, le=2_147_483_646)
    input_digest: str = Field(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class SyncSetupHandoffView(TZAwareBaseModel):
    """A credential for a fixed setup-only entry point, not generic billing."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["sync-setup-handoff-v1"] = "sync-setup-handoff-v1"
    token_type: Literal["sync_addon_setup"] = "sync_addon_setup"  # noqa: S105
    token: str = Field(min_length=1, max_length=8192, repr=False)
    kid: str = Field(min_length=1, max_length=128)
    jti: str = Field(min_length=32, max_length=128)
    expires_at: datetime
    url: str = Field(min_length=1, max_length=12_288, repr=False)
