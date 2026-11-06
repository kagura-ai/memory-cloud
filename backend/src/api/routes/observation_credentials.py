"""Issue, list, rotate and revoke observation credentials (#2679).

The workspace owner (session or owner API key) manages the read-only
credentials a self-hosted dashboard uses for ``/api/v1/observability/*``. A
credential is bound to the owner's current workspace, or to one resource of
it, and always expires. The plaintext secret is in the create and rotate
responses only; it is never stored or shown again.

These routes work while ``OBSERVABILITY_API_ENABLED`` is off, so an operator
can prepare credentials before switching the API on. Every lookup is by
``(public_id, workspace_id)``: another workspace's credential is a 404.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceOwner
from auth.observation_credentials import (
    OBSERVATION_KEY_DEFAULT_TTL_DAYS,
    OBSERVATION_KEY_MAX_TTL_DAYS,
    OBSERVATION_KEY_MIN_TTL_DAYS,
    OBSERVATION_ROTATE_MAX_GRACE_SECONDS,
    ObservationCredentialManager,
    credential_status,
)
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.observation_credential import OBSERVATION_NAME_MAX_CHARS, ObservationCredential
from models.resource import Resource
from services.resource_lookup import resolve_resource_pk
from utils.exceptions import NotFoundException
from utils.public_id import ObservationCredentialPublicId, PublicIdPrefix, public_id_pattern

router = APIRouter(prefix="/observation-credentials", tags=["observation-credentials"])


class ObservationCredentialCreate(BaseModel):
    """Request body for issuing a credential."""

    name: str = Field(..., min_length=1, max_length=OBSERVATION_NAME_MAX_CHARS)
    scope: Literal["workspace", "resource"] = Field(
        "workspace",
        description="`workspace`: every resource of the current workspace; "
        "`resource`: the one resource named by `resource_id`",
    )
    resource_id: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        description="Resource slug; required for `scope=resource`, refused otherwise",
    )
    ttl_days: int = Field(
        OBSERVATION_KEY_DEFAULT_TTL_DAYS,
        ge=OBSERVATION_KEY_MIN_TTL_DAYS,
        le=OBSERVATION_KEY_MAX_TTL_DAYS,
        description="Lifetime in days (1-365, default 90); every credential expires",
    )

    @model_validator(mode="after")
    def _scope_matches_resource(self) -> ObservationCredentialCreate:
        if (self.scope == "resource") != (self.resource_id is not None):
            raise ValueError("resource_id is required for scope=resource and only for it")
        return self


class ObservationCredentialRotate(BaseModel):
    """Request body for rotating a credential."""

    grace_seconds: int = Field(
        0,
        ge=0,
        le=OBSERVATION_ROTATE_MAX_GRACE_SECONDS,
        description="How long the old secret keeps working (0-86400 s, default 0)",
    )


class ObservationCredentialResponse(TZAwareBaseModel):
    """A credential's metadata (never the secret)."""

    id: str = Field(
        ...,
        description="Public id (`obsk_` + 22 base62 characters)",
        pattern=public_id_pattern(PublicIdPrefix.OBSERVATION_KEY),
    )
    name: str
    key_prefix: str = Field(..., description="The secret's first 16 characters")
    scope: Literal["workspace", "resource"]
    resource_id: str | None = Field(None, description="The bound resource's slug")
    created_by: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    rotated_from: str | None = Field(None, description="Public id of the credential it replaced")
    status: Literal["active", "expired", "revoked"]


class ObservationCredentialCreateResponse(ObservationCredentialResponse):
    """A new credential with its secret, shown only in this response."""

    secret: str = Field(..., description="The bearer secret (`kagura_obs_…`); shown only once")


async def _slugs(db: AsyncSession, rows: list[ObservationCredential]) -> dict[UUID, str]:
    pks = {r.resource_pk for r in rows if r.resource_pk is not None}
    if not pks:
        return {}
    result = await db.execute(select(Resource.id, Resource.resource_id).where(Resource.id.in_(pks)))
    return dict(result.tuples().all())


async def _public_ids(db: AsyncSession, rows: list[ObservationCredential]) -> dict[int, str]:
    ids = {r.rotated_from_id for r in rows if r.rotated_from_id is not None}
    if not ids:
        return {}
    result = await db.execute(
        select(ObservationCredential.id, ObservationCredential.public_id).where(
            ObservationCredential.id.in_(ids)
        )
    )
    return dict(result.tuples().all())


def _response_fields(
    row: ObservationCredential, slugs: dict[UUID, str], public_ids: dict[int, str]
) -> dict:
    return {
        "id": row.public_id,
        "name": row.name,
        "key_prefix": row.key_prefix,
        "scope": row.scope,
        "resource_id": slugs.get(row.resource_pk) if row.resource_pk else None,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "last_used_at": row.last_used_at,
        "revoked_at": row.revoked_at,
        "rotated_from": public_ids.get(row.rotated_from_id) if row.rotated_from_id else None,
        "status": credential_status(row),
    }


async def _created_response(
    db: AsyncSession, row: ObservationCredential, secret: str
) -> ObservationCredentialCreateResponse:
    await db.refresh(row)
    fields = _response_fields(row, await _slugs(db, [row]), await _public_ids(db, [row]))
    return ObservationCredentialCreateResponse(**fields, secret=secret)


@router.post(
    "",
    response_model=ObservationCredentialCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_observation_credential(
    body: ObservationCredentialCreate,
    owner: WorkspaceOwner,
    db: AsyncSession = Depends(get_db),
) -> ObservationCredentialCreateResponse:
    """Issue a read-only observation credential for the current workspace.

    Raises:
        404: ``scope=resource`` names no resource of the current workspace.
    """
    user_id, workspace_id = owner
    resource_pk = None
    if body.scope == "resource":
        assert body.resource_id is not None  # the model validator guarantees it
        resource_pk = await resolve_resource_pk(db, workspace_id, body.resource_id)
        if resource_pk is None:
            raise NotFoundException("Resource")
    row, secret = await ObservationCredentialManager(db).create(
        workspace_id,
        name=body.name,
        resource_pk=resource_pk,
        ttl_days=body.ttl_days,
        created_by=user_id,
    )
    await db.commit()
    return await _created_response(db, row, secret)


@router.get("", response_model=list[ObservationCredentialResponse])
async def list_observation_credentials(
    owner: WorkspaceOwner,
    db: AsyncSession = Depends(get_db),
) -> list[ObservationCredentialResponse]:
    """The current workspace's credentials, expired and revoked ones included."""
    _, workspace_id = owner
    rows = await ObservationCredentialManager(db).list_for_workspace(workspace_id)
    slugs = await _slugs(db, rows)
    public_ids = await _public_ids(db, rows)
    return [ObservationCredentialResponse(**_response_fields(r, slugs, public_ids)) for r in rows]


@router.post(
    "/{credential_id}/rotate",
    response_model=ObservationCredentialCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def rotate_observation_credential(
    credential_id: ObservationCredentialPublicId,
    owner: WorkspaceOwner,
    body: ObservationCredentialRotate | None = None,
    db: AsyncSession = Depends(get_db),
) -> ObservationCredentialCreateResponse:
    """Replace a live credential with a new secret of the same binding.

    The new credential keeps the old one's expiry; the old secret stops
    working after ``grace_seconds``.

    Raises:
        404: No live credential with this id in the current workspace, or
            it was already rotated.
    """
    user_id, workspace_id = owner
    grace = body.grace_seconds if body is not None else 0
    rotated = await ObservationCredentialManager(db).rotate(
        credential_id, workspace_id, grace_seconds=grace, created_by=user_id
    )
    if rotated is None:
        raise NotFoundException("Observation credential")
    row, secret = rotated
    await db.commit()
    return await _created_response(db, row, secret)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoke_observation_credential(
    credential_id: ObservationCredentialPublicId,
    owner: WorkspaceOwner,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a credential (revoking it again is a no-op).

    Raises:
        404: No credential with this id in the current workspace.
    """
    _, workspace_id = owner
    if not await ObservationCredentialManager(db).revoke(credential_id, workspace_id):
        raise NotFoundException("Observation credential")
    await db.commit()
