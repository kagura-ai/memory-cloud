"""Zero-knowledge secret store routes (#1128).

Server side of a passive, ciphertext-only secret store. The server holds public
``age`` recipient keys + opaque ciphertext only and never decrypts. Surfaces:

- **Management** (owner/admin): register-approval of recipient pubkeys, put /
  list / revoke-grant of secrets. Owner-only for the pubkey trust gate
  (approve/revoke) — the TOFU attestation root.
- **Consumption** (member + grant): ``/fetch`` returns ciphertext to a caller
  who holds an active grant via an active pubkey. The service enforces
  default-deny and writes a fail-closed audit entry before returning ciphertext.

The UI surface is intentionally **revoke-only / value-free**: ``list`` returns
metadata, never values; there is no endpoint that displays plaintext (the server
has none). Decryption happens entirely client-side in the SDK.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceAdmin, WorkspaceMember, WorkspaceOwner
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.secrets import PUBKEY_LABEL_MAX_LEN, PUBKEY_MAX_LEN, SECRET_NAME_MAX_LEN
from services.secret_store_service import (
    CIPHERTEXT_MAX_LEN,
    SecretAccessDenied,
    SecretNotFound,
    SecretStoreService,
)
from utils.auth_helpers import get_user_id
from utils.exceptions import AuthorizationError, BadRequestError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/config/secrets", tags=["secrets"])


# ============================================================================
# Dependency Injection
# ============================================================================


async def get_secret_service(db: AsyncSession = Depends(get_db)) -> SecretStoreService:
    """Get a SecretStoreService bound to the request session."""
    return SecretStoreService(db)


def _ws_id(user: dict) -> UUID:
    """Extract the verified current workspace id from an auth dict."""
    raw = user.get("current_workspace_id")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _rest_meta(request: Request) -> dict:
    """Non-secret request metadata for the audit log (never the secret value)."""
    return {
        "source": "rest",
        "ip": request.client.host if request.client else None,
        "ua": request.headers.get("user-agent"),
    }


# ============================================================================
# Pydantic Models
# ============================================================================


class PubkeyRegister(BaseModel):
    """Register the caller's own age recipient pubkey (becomes pending)."""

    pubkey: str = Field(
        ..., min_length=8, max_length=PUBKEY_MAX_LEN, description="age recipient public key (age1…)"
    )
    label: str | None = Field(None, max_length=PUBKEY_LABEL_MAX_LEN, description="Friendly label")


class PubkeyResponse(TZAwareBaseModel):
    """Recipient pubkey metadata (the pubkey is public; no private material)."""

    id: UUID
    identity_id: str
    pubkey: str
    fingerprint: str
    label: str | None
    status: Literal["pending", "active", "revoked"]
    created_at: datetime
    attested_at: datetime | None
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


class SecretPut(BaseModel):
    """Store a new ciphertext version of a secret. Server never sees plaintext."""

    name: str = Field(..., min_length=1, max_length=SECRET_NAME_MAX_LEN)
    ciphertext: str = Field(
        ..., min_length=1, max_length=CIPHERTEXT_MAX_LEN, description="Armored age ciphertext"
    )
    recipients_snapshot: list[str] = Field(
        ..., min_length=1, description="Fingerprints the ciphertext was encrypted to"
    )
    grant_pubkey_ids: list[UUID] = Field(
        ..., min_length=1, description="Recipient pubkey ids to grant (must be active)"
    )


class SecretFetch(BaseModel):
    """Fetch a secret's ciphertext (name carried in body to allow '/' names)."""

    name: str = Field(..., min_length=1, max_length=SECRET_NAME_MAX_LEN)
    version_number: int | None = Field(None, ge=1, description="Pin a version; omit for latest")


class RevokeGrant(BaseModel):
    """Revoke one recipient's grant on a secret (flags rotation_needed)."""

    name: str = Field(..., min_length=1, max_length=SECRET_NAME_MAX_LEN)
    recipient_pubkey_id: UUID


class SecretMetaResponse(TZAwareBaseModel):
    """Secret metadata — never includes the value."""

    name: str
    status: str
    rotation_needed: bool
    current_version: int | None
    grant_count: int
    created_at: datetime
    updated_at: datetime | None


class SecretPutResponse(TZAwareBaseModel):
    """Result of a put."""

    name: str
    version_number: int
    status: str
    rotation_needed: bool


class SecretValueResponse(TZAwareBaseModel):
    """Opaque ciphertext returned to a granted caller. The server cannot read it."""

    name: str
    version_number: int
    alg: str
    ciphertext: str | None
    blob_ref: str | None
    recipients_snapshot: list[str]
    rotation_needed: bool
    created_at: datetime


# ============================================================================
# Recipient pubkeys
# ============================================================================


@router.post("/pubkeys", response_model=PubkeyResponse, status_code=status.HTTP_201_CREATED)
async def register_pubkey(
    data: PubkeyRegister,
    user: WorkspaceMember,
    svc: SecretStoreService = Depends(get_secret_service),
    db: AsyncSession = Depends(get_db),
) -> PubkeyResponse:
    """Register the caller's own age recipient pubkey (pending owner approval)."""
    user_id = get_user_id(user)
    try:
        row = await svc.register_pubkey(
            workspace_id=_ws_id(user),
            actor_user_id=user_id,
            pubkey=data.pubkey,
            label=data.label,
        )
    except ValueError as e:
        raise BadRequestError(str(e)) from e
    await db.commit()
    return PubkeyResponse.model_validate(row)


@router.get("/pubkeys", response_model=list[PubkeyResponse])
async def list_pubkeys(
    user: WorkspaceAdmin,
    svc: SecretStoreService = Depends(get_secret_service),
) -> list[PubkeyResponse]:
    """List all recipient pubkeys in the workspace (owner/admin approval console)."""
    rows = await svc.list_pubkeys(workspace_id=_ws_id(user))
    return [PubkeyResponse.model_validate(r) for r in rows]


@router.get("/pubkeys/me", response_model=list[PubkeyResponse])
async def list_my_pubkeys(
    user: WorkspaceMember,
    svc: SecretStoreService = Depends(get_secret_service),
) -> list[PubkeyResponse]:
    """List the caller's own recipient pubkeys (e.g. to check approval status)."""
    rows = await svc.list_pubkeys(workspace_id=_ws_id(user), identity_id=get_user_id(user))
    return [PubkeyResponse.model_validate(r) for r in rows]


@router.post("/pubkeys/{pubkey_id}/approve", response_model=PubkeyResponse)
async def approve_pubkey(
    pubkey_id: UUID,
    owner: WorkspaceOwner,
    svc: SecretStoreService = Depends(get_secret_service),
    db: AsyncSession = Depends(get_db),
) -> PubkeyResponse:
    """Owner-approve a pending pubkey → active (the TOFU trust gate)."""
    user_id, workspace_id = owner
    try:
        row = await svc.approve_pubkey(
            workspace_id=workspace_id, actor_user_id=user_id, pubkey_id=pubkey_id
        )
    except SecretNotFound as e:
        raise NotFoundException("Recipient pubkey") from e
    except ValueError as e:
        raise BadRequestError(str(e)) from e
    await db.commit()
    return PubkeyResponse.model_validate(row)


@router.post("/pubkeys/{pubkey_id}/revoke", response_model=PubkeyResponse)
async def revoke_pubkey(
    pubkey_id: UUID,
    owner: WorkspaceOwner,
    svc: SecretStoreService = Depends(get_secret_service),
    db: AsyncSession = Depends(get_db),
) -> PubkeyResponse:
    """Owner-revoke a pubkey; revokes its grants and flags rotation on affected secrets."""
    user_id, workspace_id = owner
    try:
        row = await svc.revoke_pubkey(
            workspace_id=workspace_id, actor_user_id=user_id, pubkey_id=pubkey_id
        )
    except SecretNotFound as e:
        raise NotFoundException("Recipient pubkey") from e
    except ValueError as e:
        raise BadRequestError(str(e)) from e
    await db.commit()
    return PubkeyResponse.model_validate(row)


# ============================================================================
# Secrets — management (owner/admin)
# ============================================================================


@router.post("", response_model=SecretPutResponse, status_code=status.HTTP_201_CREATED)
async def put_secret(
    data: SecretPut,
    request: Request,
    user: WorkspaceAdmin,
    svc: SecretStoreService = Depends(get_secret_service),
    db: AsyncSession = Depends(get_db),
) -> SecretPutResponse:
    """Store a new ciphertext version + reconcile grants. Server never sees plaintext."""
    try:
        secret, version = await svc.put_secret(
            workspace_id=_ws_id(user),
            actor_user_id=get_user_id(user),
            name=data.name,
            ciphertext=data.ciphertext,
            recipients_snapshot=data.recipients_snapshot,
            grant_pubkey_ids=data.grant_pubkey_ids,
            req_meta=_rest_meta(request),
        )
    except ValueError as e:
        raise BadRequestError(str(e)) from e
    await db.commit()
    return SecretPutResponse(
        name=secret.name,
        version_number=version.version_number,
        status=secret.status,
        rotation_needed=secret.rotation_needed,
    )


@router.get("", response_model=list[SecretMetaResponse])
async def list_secrets(
    user: WorkspaceAdmin,
    svc: SecretStoreService = Depends(get_secret_service),
) -> list[SecretMetaResponse]:
    """List secret names + metadata. Never returns ciphertext/values."""
    rows = await svc.list_secrets(workspace_id=_ws_id(user))
    return [SecretMetaResponse(**r) for r in rows]


class AuditVerifyResponse(BaseModel):
    """Result of recomputing the workspace's tamper-evident audit chain."""

    valid: bool
    entries: int | None = None
    head: str | None = None
    broken_at: int | None = None
    reason: str | None = None


@router.get("/audit/verify", response_model=AuditVerifyResponse)
async def verify_audit_chain(
    user: WorkspaceAdmin,
    svc: SecretStoreService = Depends(get_secret_service),
) -> AuditVerifyResponse:
    """Recompute the workspace's secret-access audit chain and report integrity.

    Owner/admin ops surface that makes the tamper-evidence usable in-product:
    walks the append-only log id-ascending, recomputes each HMAC ``entry_hash``
    and verifies the ``prev_hash`` linkage. Returns ``valid: true`` with the head
    hash, or ``valid: false`` with the first ``broken_at`` id and ``reason``.
    """
    result = await svc.verify_audit_chain(workspace_id=_ws_id(user))
    return AuditVerifyResponse(**result)


@router.post("/revoke-grant", response_model=SecretMetaResponse)
async def revoke_grant(
    data: RevokeGrant,
    user: WorkspaceAdmin,
    svc: SecretStoreService = Depends(get_secret_service),
    db: AsyncSession = Depends(get_db),
) -> SecretMetaResponse:
    """Revoke one recipient's grant; sets rotation_needed (revoke ≠ un-share)."""
    try:
        await svc.revoke_grant(
            workspace_id=_ws_id(user),
            actor_user_id=get_user_id(user),
            name=data.name,
            recipient_pubkey_id=data.recipient_pubkey_id,
        )
    except SecretNotFound as e:
        raise NotFoundException("Secret or active grant") from e
    await db.commit()
    rows = await svc.list_secrets(workspace_id=_ws_id(user))
    match = next((r for r in rows if r["name"] == data.name), None)
    if match is None:  # pragma: no cover - defensive (revoke just succeeded)
        raise NotFoundException("Secret")
    return SecretMetaResponse(**match)


# ============================================================================
# Secrets — consumption (member + grant)
# ============================================================================


@router.post("/fetch", response_model=SecretValueResponse)
async def fetch_secret(
    data: SecretFetch,
    request: Request,
    user: WorkspaceMember,
    svc: SecretStoreService = Depends(get_secret_service),
) -> SecretValueResponse:
    """Return ciphertext to a granted caller (default-deny, audit-first).

    The service writes and commits a fail-closed audit entry before the
    ciphertext is returned; a denied read raises 403 after logging. The returned
    ciphertext is opaque — the server holds no key to decrypt it.
    """
    try:
        payload = await svc.get_secret(
            workspace_id=_ws_id(user),
            actor_user_id=get_user_id(user),
            name=data.name,
            version_number=data.version_number,
            req_meta=_rest_meta(request),
        )
    except SecretAccessDenied as e:
        raise AuthorizationError(str(e)) from e
    except SecretNotFound as e:
        raise NotFoundException("Secret version") from e
    return SecretValueResponse(**payload)
