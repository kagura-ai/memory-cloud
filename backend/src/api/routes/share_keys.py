"""Share-key routes — context-scoped, read-only, TTL-bounded share keys (#1027).

Two surfaces, deliberately separated so the security model is structural:

- ``router`` (``/config/share-keys``) — management, **session-authenticated**.
  Mint / list / revoke. Minting requires the caller to OWN the bound context.
- ``recall_router`` (``/share``) — the single **share-key-authenticated** read
  surface. A share key is honored here and nowhere else; every other endpoint
  authenticates against ``api_keys`` and rejects a share key outright
  (fail-closed allow-list, not a verb deny-list).

Recall through a share key is confined to the bound context: the bound context
is forced as the recall context, and a client-supplied ``context_id`` that
differs is rejected (mirrors ``_resolve_public_attribution``'s
BOUND_SCOPE_VIOLATION, #963/#150 non-regression).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser, ShareKeyUser
from auth.share_keys import ShareKeyManager
from auth.workspace_roles import ContextRole
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import ShareKey
from models.schemas import RecallRequest, RecallResponse
from services.memory_service import MemoryService
from services.permission_service import PermissionService
from utils.auth_helpers import get_user_id
from utils.datetime import utcnow
from utils.exceptions import (
    AuthorizationError,
    BadRequestError,
    NotFoundException,
    ValidationError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Session-authenticated management surface.
router = APIRouter(prefix="/config/share-keys", tags=["share-keys"])
# Share-key-authenticated read surface (the ONLY place a share key is honored).
recall_router = APIRouter(prefix="/share", tags=["share-keys"])


# ============================================================================
# Dependency Injection
# ============================================================================


async def get_share_key_manager(db: AsyncSession = Depends(get_db)) -> ShareKeyManager:
    """Get a ShareKeyManager bound to the request session."""
    return ShareKeyManager(db)


async def get_memory_service(db: AsyncSession = Depends(get_db)) -> MemoryService:
    """Get a MemoryService bound to the request session."""
    return MemoryService(db)


# ============================================================================
# Pydantic Models
# ============================================================================


class ShareKeyCreate(BaseModel):
    """Request to mint a share key bound to one owned context."""

    name: str = Field(..., min_length=1, max_length=100, description="Friendly name")
    context_id: UUID = Field(
        ..., description="The single context to confine the key to (must be owned)"
    )
    ttl_days: int | None = Field(
        None,
        ge=1,
        le=3650,
        description="Requested lifetime in days. Clamped server-side to a 30-day ceiling; omit for the 30-day default.",
    )


class ShareKeyResponse(TZAwareBaseModel):
    """Share-key metadata (never includes the secret)."""

    id: int = Field(..., description="Database ID")
    key_prefix: str = Field(..., description="First 16 characters of the key (display only)")
    name: str = Field(..., description="Friendly name")
    user_id: str = Field(..., description="Minting owner")
    context_id: UUID = Field(..., description="The single context this key is confined to")
    scope: str = Field(..., description="Always 'memory:read'")
    created_at: datetime = Field(..., description="Creation timestamp")
    last_used_at: datetime | None = Field(None, description="Last usage timestamp")
    revoked_at: datetime | None = Field(None, description="Revocation timestamp")
    expires_at: datetime = Field(..., description="Expiry timestamp (always set)")
    status: Literal["active", "revoked", "expired"] = Field(..., description="Current status")

    model_config = {"from_attributes": True}


class ShareKeyCreateResponse(ShareKeyResponse):
    """Mint response — includes the plaintext key ONCE."""

    share_key: str = Field(
        ..., description="Plaintext share key (ONLY shown once — must be saved by the client)"
    )


# ============================================================================
# Helpers
# ============================================================================


def _determine_status(
    revoked_at: datetime | None, expires_at: datetime
) -> Literal["active", "revoked", "expired"]:
    """Derive display status. Revoked wins over expired."""
    if revoked_at:
        return "revoked"
    if utcnow() > expires_at:
        return "expired"
    return "active"


def _format_key_response(key: ShareKey) -> ShareKeyResponse:
    """Format a ShareKey row into a ShareKeyResponse."""
    return ShareKeyResponse(
        id=key.id,
        key_prefix=key.key_prefix,
        name=key.name,
        user_id=key.user_id,
        context_id=key.context_id,
        scope=key.scope,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        expires_at=key.expires_at,
        status=_determine_status(key.revoked_at, key.expires_at),
    )


# ============================================================================
# Management routes (session-authenticated)
# ============================================================================


@router.post("", response_model=ShareKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_share_key(
    data: ShareKeyCreate,
    user: SessionUser,
    manager: ShareKeyManager = Depends(get_share_key_manager),
    db: AsyncSession = Depends(get_db),
) -> ShareKeyCreateResponse:
    """Mint a read-only, TTL-bounded share key for one owned context (#1027).

    The caller must OWN ``context_id`` — enforced via
    ``check_context_access(..., OWNER)``, which raises a uniform 404 if the
    context does not exist, and 403 if the caller lacks owner access (whether
    they are a non-member or a member without owner rights — the 403 does not
    distinguish, to avoid leaking membership). This prevents minting a share
    key for a context the user cannot administer.
    """
    user_id = get_user_id(user)

    # Ownership gate — must be able to OWNER-access the bound context.
    perm = PermissionService(db)
    await perm.check_context_access(user_id, data.context_id, required_role=ContextRole.OWNER)

    try:
        share_key, created = await manager.create_key(
            name=data.name,
            user_id=user_id,
            context_id=data.context_id,
            ttl_days=data.ttl_days,
        )
    except ValueError as e:
        raise BadRequestError(str(e)) from e

    await db.commit()
    response_data = _format_key_response(created)
    return ShareKeyCreateResponse(**response_data.model_dump(), share_key=share_key)


@router.get("", response_model=list[ShareKeyResponse])
async def list_share_keys(
    user: SessionUser,
    manager: ShareKeyManager = Depends(get_share_key_manager),
) -> list[ShareKeyResponse]:
    """List the current user's share keys (newest first)."""
    user_id = get_user_id(user)
    keys = await manager.list_keys(user_id=user_id)
    return [_format_key_response(k) for k in keys]


@router.post("/{key_id}/revoke", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoke_share_key(
    key_id: int,
    user: SessionUser,
    manager: ShareKeyManager = Depends(get_share_key_manager),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a share key (soft delete; uniform 404 if not the user's active key)."""
    user_id = get_user_id(user)
    revoked = await manager.revoke_key(key_id=key_id, user_id=user_id)
    if not revoked:
        raise NotFoundException("Share key not found or not owned by you")
    await db.commit()


# ============================================================================
# Read surface (share-key-authenticated) — the ONLY place a share key works
# ============================================================================


@recall_router.post("/recall", response_model=RecallResponse, response_model_exclude_none=True)
async def share_recall(
    request: RecallRequest,
    principal: ShareKeyUser,
    memory_service: MemoryService = Depends(get_memory_service),
) -> RecallResponse:
    """Recall confined to the share key's bound context (#1027).

    The bound context (and its workspace) come from the verified share key —
    never from the request or the owner's current workspace. A client-supplied
    ``filters.context_id`` that differs from the bound context is rejected
    (BOUND_SCOPE_VIOLATION), and otherwise recall is forced to the bound
    context so cross-context read is structurally impossible (#963/#150).
    """
    bound_context_id: UUID = principal["share_key_context_id"]

    # Reject an attempt to point a share key at a different context. A
    # malformed context_id is a 422 (bad input), not a 403 (scope violation),
    # so callers get an accurate signal; a well-formed but different context is
    # the BOUND_SCOPE_VIOLATION 403.
    requested = request.filters.get("context_id") if request.filters else None
    if requested is not None:
        try:
            requested_uuid = requested if isinstance(requested, UUID) else UUID(str(requested))
        except (ValueError, TypeError) as e:
            raise ValidationError("Invalid context_id", field="context_id") from e
        if requested_uuid != bound_context_id:
            logger.warning(
                "share_key_bound_scope_violation",
                share_key_id=principal.get("share_key_id"),
                bound_context_id=str(bound_context_id),
            )
            raise AuthorizationError("Share key is bound to a different context")

    try:
        result = await memory_service.recall(
            request,
            user_id=principal["user_id"],
            current_context_id=bound_context_id,
            current_workspace_id=principal["current_workspace_id"],
        )
    except ValueError as e:
        raise ValidationError(str(e)) from e

    return result
