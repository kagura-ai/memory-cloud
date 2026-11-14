"""Fixed, non-expanding input and inspection contracts for synchronization."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from models.api_base import TZAwareBaseModel


class SyncAuthorizationCreate(TZAwareBaseModel):
    """Approve a server-registered service; no client, operation or target override."""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    application_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    public_name: str = Field(min_length=1, max_length=200)
    public_description: str | None = Field(default=None, max_length=2000)
    public_usage_guide: str | None = Field(default=None, max_length=4000)

    @field_validator("public_name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        """Require explicit public-facing text, without implicit internal fallbacks."""
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Public name must be nonblank text without controls")
        return value.strip()


class SyncActivation(TZAwareBaseModel):
    """Reference one exact recorded application consent; never select a target."""

    model_config = ConfigDict(extra="forbid")
    authorization_id: UUID
    application_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class SyncAuthorizationView(TZAwareBaseModel):
    """Stored approval, with no synchronization credential."""

    authorization_id: UUID
    workspace_id: UUID
    application_id: str
    service_id: str
    operations: list[str]
    ownership_epoch: int
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    used_at: datetime | None


class SyncInstallationView(TZAwareBaseModel):
    """Stable workspace target and lifecycle facts, never an authorization grant."""

    installation_id: UUID
    workspace_id: UUID
    service_id: str
    service_client_id: str
    application_id: str
    resource_pk: UUID
    context_id: UUID
    resource_id: str
    endpoint_id: UUID
    public_id: str | None
    public_path: str | None
    target_available: bool
    state: Literal["active", "paused", "revoked"]
    generation: int
    operations: list[str]
    created_at: datetime
    revoked_at: datetime | None
