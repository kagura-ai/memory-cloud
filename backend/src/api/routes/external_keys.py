"""External API Keys Management Routes.

Manage external API keys (OpenAI, Cohere, etc.) with Fernet encryption.
Issue #45: Web UI Endpoint Implementation
Issue #106: Refactored to use consolidated utilities
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser, require_workspace_owner
from db.base import get_db
from models.auth import ExternalAPIKey
from utils import db_transaction, get_user_email, mask_secret
from utils.logger import get_logger

logger = get_logger(__name__)

# Issue #381: All external API key routes are owner-only.
# External API keys are workspace-level secrets (OpenAI/Cohere/Anthropic credentials)
# that should only be managed by the workspace owner. Viewers, members, and admins
# cannot list/create/update/toggle/delete keys.
router = APIRouter(
    prefix="/external-keys",
    tags=["external-keys"],
    dependencies=[Depends(require_workspace_owner)],
)


# ============================================================================
# Schemas
# ============================================================================


class ExternalKeyCreate(BaseModel):
    """Create external API key request."""

    key_name: str
    provider: str
    value: str
    enabled: bool = True  # Issue #105


class ExternalKeyUpdate(BaseModel):
    """Update external API key request."""

    value: str


class ExternalKeyToggle(BaseModel):
    """Toggle enabled/disabled state (Issue #105)."""

    enabled: bool


class ExternalKeyResponse(BaseModel):
    """External API key response (masked)."""

    id: int
    key_name: str
    provider: str
    masked_value: str
    user_id: str
    enabled: bool  # Issue #105
    created_at: str
    updated_at: str


class ExternalKeyListResponse(BaseModel):
    """External API keys list response."""

    keys: list[ExternalKeyResponse]
    total: int


# ============================================================================
# Encryption Helpers (use API_KEY_SECRET via get_encryptor)
# ============================================================================


def encrypt_value(value: str) -> str:
    """Encrypt API key value using API_KEY_SECRET."""
    from utils.encryption import get_encryptor

    return get_encryptor().encrypt(value)


def decrypt_value(encrypted: str) -> str:
    """Decrypt API key value using API_KEY_SECRET."""
    from utils.encryption import get_encryptor

    return get_encryptor().decrypt(encrypted)


# ============================================================================
# Validation Logic (Issue #105)
# ============================================================================

RERANKER_PROVIDERS = {"cohere", "voyage"}
EMBEDDING_PROVIDERS = {"openai"}


async def validate_reranker_exclusivity(
    db: AsyncSession,
    workspace_id,
    provider: str,
    enabled: bool,
    exclude_key_id: int | None = None,
) -> None:
    """Ensure only ONE reranker (Cohere OR Voyage) is enabled at a time per workspace.

    Issue #105: Reranker exclusivity validation.
    Issue #385: scoped per workspace — was per user before, but external keys are
        workspace-shared resources now and the partial unique index
        uq_external_api_keys_workspace_provider_enabled enforces the same invariant
        at the DB level. This application-layer check produces a friendlier 409
        with provider details.

    Rules:
    - OpenAI cannot be disabled (embeddings required)
    - Only ONE of Cohere/Voyage enabled per workspace
    - Disabling rerankers is always allowed

    Args:
        db: Database session
        workspace_id: Workspace UUID — the conflict check is scoped to this workspace.
        provider: Provider name (openai, cohere, voyage)
        enabled: Desired enabled state
        exclude_key_id: Key ID to exclude from conflict check (for updates)

    Raises:
        HTTPException: 400 if trying to disable OpenAI, 409 if reranker conflict
    """
    # Prevent disabling OpenAI (embeddings required)
    if provider in EMBEDDING_PROVIDERS and not enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "cannot_disable_embeddings",
                "message": "OpenAI embedding keys cannot be disabled. They are required for core functionality.",
            },
        )

    # Only validate reranker providers (Cohere/Voyage)
    if not enabled or provider not in RERANKER_PROVIDERS:
        return

    # Check for a conflicting enabled reranker in the same workspace.
    conditions = [
        ExternalAPIKey.workspace_id == workspace_id,
        ExternalAPIKey.enabled.is_(True),
        ExternalAPIKey.provider.in_(RERANKER_PROVIDERS),
        ExternalAPIKey.provider != provider,
    ]

    if exclude_key_id:
        conditions.append(ExternalAPIKey.id != exclude_key_id)

    result = await db.execute(select(ExternalAPIKey).where(and_(*conditions)))
    conflicting_key = result.scalar_one_or_none()

    if conflicting_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "reranker_provider_conflict",
                "message": f"Cannot enable {provider.upper()} because "
                f"{conflicting_key.provider.upper()} is already enabled. "
                f"Only ONE reranker (Cohere OR Voyage) can be active at a time.",
                "conflicting_provider": conflicting_key.provider,
                "conflicting_key_name": conflicting_key.key_name,
            },
        )


# ============================================================================
# Endpoints
# ============================================================================


@router.get("", response_model=ExternalKeyListResponse)
async def list_external_keys(
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """List all external API keys for the current workspace.

    Issue #82: Now context-scoped - returns external keys for current context only.
    Issue #246: current_context_id removed - show all user keys
    Issue #381: Owner-only (router-level dependency); members, admins, and viewers
    are rejected with 403 before reaching this handler.

    Returns masked values for security.
    """
    user_id = user.get("user_id")
    current_workspace_id = user.get("current_workspace_id")

    async with db_transaction(db, "list_external_keys", "Failed to list external API keys"):
        # Issue #385: workspace_id is NOT NULL — every key belongs to exactly one workspace.
        # The router-level require_workspace_owner dep guarantees current_workspace_id is set.
        result = await db.execute(
            select(ExternalAPIKey)
            .where(ExternalAPIKey.workspace_id == current_workspace_id)
            .order_by(ExternalAPIKey.created_at.desc())
        )
        keys = list(result.scalars().all())

        # Decrypt and mask values
        key_responses = []
        for key in keys:
            try:
                decrypted = decrypt_value(key.encrypted_value)
                masked = mask_secret(decrypted)
            except Exception:
                masked = "***ERROR***"

            key_responses.append(
                ExternalKeyResponse(
                    id=key.id,
                    key_name=key.key_name,
                    provider=key.provider,
                    masked_value=masked,
                    user_id=key.user_id,
                    enabled=key.enabled,  # Issue #105
                    created_at=key.created_at.isoformat(),
                    updated_at=key.updated_at.isoformat(),
                )
            )

        logger.info("external_keys_listed", user_id=user_id, count=len(keys))

        return ExternalKeyListResponse(keys=key_responses, total=len(keys))


@router.post("", response_model=ExternalKeyResponse)
async def create_external_key(
    request: ExternalKeyCreate,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Create a new external API key for the current workspace.

    Issue #82: Auto-assigns external key to current context.
    Issue #246: current_context_id removed - use context_id=None
    Issue #381: Owner-only (router-level dependency).
    """
    user_id = user.get("user_id")
    user_email = get_user_email(user) or user_id
    current_workspace_id = user.get("current_workspace_id")

    async with db_transaction(db, "create_external_key", "Failed to create external API key"):
        # Issue #385: workspace-scoped duplicate check — name uniqueness is per-workspace.
        # The partial unique index (workspace_id, provider) WHERE enabled=true gives DB-level
        # enforcement on top; the application-layer check below produces a friendlier 409.
        result = await db.execute(
            select(ExternalAPIKey).where(
                and_(
                    ExternalAPIKey.key_name == request.key_name,
                    ExternalAPIKey.workspace_id == current_workspace_id,
                )
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Issue #223: Don't reveal key name in error message for security
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An API key with this configuration already exists",
            )

        # Validate reranker exclusivity per workspace (Issue #105 / #385).
        await validate_reranker_exclusivity(
            db=db,
            workspace_id=current_workspace_id,
            provider=request.provider,
            enabled=request.enabled,
        )

        # Encrypt value and create key
        encrypted = encrypt_value(request.value)
        new_key = ExternalAPIKey(
            key_name=request.key_name,
            provider=request.provider,
            encrypted_value=encrypted,
            user_id=user_id,
            context_id=None,  # Issue #246: No context assignment
            workspace_id=current_workspace_id,  # Issue #146: Workspace-scoped keys
            enabled=request.enabled,  # Issue #105
            updated_by=user_email,
        )

        db.add(new_key)
        await db.commit()
        await db.refresh(new_key)

        logger.info(
            f"external_key_created: key_name={request.key_name}, "
            f"provider={request.provider}, user={user_id}"
        )

        return ExternalKeyResponse(
            id=new_key.id,
            key_name=new_key.key_name,
            provider=new_key.provider,
            masked_value=mask_secret(request.value),
            user_id=new_key.user_id,
            enabled=new_key.enabled,  # Issue #105
            created_at=new_key.created_at.isoformat(),
            updated_at=new_key.updated_at.isoformat(),
        )


@router.put("/{key_name}", response_model=ExternalKeyResponse)
async def update_external_key(
    key_name: str,
    request: ExternalKeyUpdate,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Update an external API key value in the current workspace.

    Issue #112: Now filters by context_id to handle duplicate key names across contexts.
    Issue #246: current_context_id removed - use context_id=None
    Issue #381: Owner-only (router-level dependency).
    """
    user_id = user.get("user_id")
    user_email = get_user_email(user) or user_id
    current_workspace_id = user.get("current_workspace_id")

    async with db_transaction(db, "update_external_key", "Failed to update external API key"):
        # Issue #385: workspace-scoped lookup. Any owner of this workspace can update
        # any key registered in it (including keys originally created by a previous
        # owner) — the previous user_id == caller filter was a creator-only check that
        # broke ownership transitions.
        result = await db.execute(
            select(ExternalAPIKey).where(
                and_(
                    ExternalAPIKey.key_name == key_name,
                    ExternalAPIKey.workspace_id == current_workspace_id,
                )
            )
        )
        key = result.scalar_one_or_none()

        if not key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"External key '{key_name}' not found",
            )

        # Encrypt and update
        key.encrypted_value = encrypt_value(request.value)
        key.updated_by = user_email

        await db.commit()
        await db.refresh(key)

        logger.info(f"external_key_updated: key_name={key_name}, user={user_id}")

        return ExternalKeyResponse(
            id=key.id,
            key_name=key.key_name,
            provider=key.provider,
            masked_value=mask_secret(request.value),
            user_id=key.user_id,
            enabled=key.enabled,  # Issue #105
            created_at=key.created_at.isoformat(),
            updated_at=key.updated_at.isoformat(),
        )


@router.patch("/{key_name}/toggle", response_model=ExternalKeyResponse)
async def toggle_external_key(
    key_name: str,
    request: ExternalKeyToggle,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Toggle enabled/disabled state without re-entering API key value.

    Issue #105: Enable/disable keys for reranker provider switching.
    Issue #246: current_context_id removed - use None

    Rules:
    - OpenAI keys cannot be disabled
    - Only ONE reranker (Cohere/Voyage) can be enabled at a time

    Issue #381: Owner-only (router-level dependency).
    """
    user_id = user.get("user_id")
    user_email = get_user_email(user) or user_id
    current_workspace_id = user.get("current_workspace_id")

    async with db_transaction(db, "toggle_external_key", "Failed to toggle external API key"):
        # Issue #385: workspace-scoped lookup (see update_external_key for rationale).
        result = await db.execute(
            select(ExternalAPIKey).where(
                and_(
                    ExternalAPIKey.key_name == key_name,
                    ExternalAPIKey.workspace_id == current_workspace_id,
                )
            )
        )
        key = result.scalar_one_or_none()

        if not key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"External key '{key_name}' not found",
            )

        # Validate reranker exclusivity per workspace (Issue #105 / #385).
        await validate_reranker_exclusivity(
            db=db,
            workspace_id=current_workspace_id,
            provider=key.provider,
            enabled=request.enabled,
            exclude_key_id=key.id,
        )

        # Update enabled state
        key.enabled = request.enabled
        key.updated_by = user_email

        await db.commit()
        await db.refresh(key)

        logger.info(
            f"external_key_toggled: key_name={key_name}, provider={key.provider}, "
            f"enabled={key.enabled}, user={user_id}"
        )

        return ExternalKeyResponse(
            id=key.id,
            key_name=key.key_name,
            provider=key.provider,
            masked_value=mask_secret(decrypt_value(key.encrypted_value)),
            user_id=key.user_id,
            enabled=key.enabled,
            created_at=key.created_at.isoformat(),
            updated_at=key.updated_at.isoformat(),
        )


@router.delete("/{key_name}")
async def delete_external_key(
    key_name: str,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Delete an external API key from the current workspace.

    Issue #112: Now filters by context_id to handle duplicate key names across contexts.
    Issue #246: current_context_id removed - no context filtering
    Issue #381: Owner-only (router-level dependency).
    """
    user_id = user.get("user_id")
    current_workspace_id = user.get("current_workspace_id")

    async with db_transaction(db, "delete_external_key", "Failed to delete external API key"):
        # Issue #385: workspace-scoped lookup (see update_external_key for rationale).
        result = await db.execute(
            select(ExternalAPIKey).where(
                and_(
                    ExternalAPIKey.key_name == key_name,
                    ExternalAPIKey.workspace_id == current_workspace_id,
                )
            )
        )
        key = result.scalar_one_or_none()

        if not key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"External key '{key_name}' not found",
            )

        # Issue #149: Prevent deletion of protected keys (required for system operations)
        from config.plan_tiers import PROTECTED_KEYS

        if key.key_name in PROTECTED_KEYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete {key.key_name}. This key is required for embedding generation and memory operations.",
            )

        await db.delete(key)
        await db.commit()

        logger.info(f"external_key_deleted: key_name={key_name}, user={user_id}")

        return {"message": f"External key '{key_name}' deleted successfully"}
