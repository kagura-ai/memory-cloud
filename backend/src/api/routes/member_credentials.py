"""Member Credentials API Routes.

Migration 034: Member-scoped API Keys and OAuth Apps with Zero-knowledge visibility.
Issue #252: Session-only authentication (no API keys)

Endpoints:
- GET /workspaces/{workspace_id}/members/{user_id}/credentials
- POST /workspaces/{workspace_id}/members/{user_id}/credentials/api-key/hide
- POST /workspaces/{workspace_id}/members/{user_id}/credentials/api-key/regenerate
- DELETE /workspaces/{workspace_id}/members/{user_id}/credentials/api-key
- Similar for OAuth apps
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import APIKeyManager
from auth.dependencies import SessionUser
from db.base import get_db
from models.schemas import (
    CreateAPIKeyRequest,
    MemberAPIKeyResponse,
    MemberCredentialsResponse,
    RegenerateAPIKeyResponse,
    RegenerateOAuthSecretResponse,
)
from services.member_credentials_service import MemberCredentialsService
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/members",
    tags=["member-credentials"],
)


# ============================================================================
# Helper Functions
# ============================================================================


async def check_permission(
    service: MemberCredentialsService,
    requester_id: str,
    workspace_id: UUID,
    target_user_id: str,
    action: str,
) -> None:
    """Check permission and raise HTTPException if not allowed.

    Args:
        service: MemberCredentialsService instance
        requester_id: Requesting user ID
        workspace_id: Workspace ID
        target_user_id: Target user ID
        action: Action to perform

    Raises:
        HTTPException: 403 if not authorized
    """
    can_perform = await service.check_can_manage(
        requester_id=requester_id,
        workspace_id=workspace_id,
        target_user_id=target_user_id,
        action=action,
    )

    if not can_perform:
        raise HTTPException(
            status_code=403,
            detail=f"Not authorized to {action} credentials",
        )


@router.get("/{user_id}/credentials", response_model=MemberCredentialsResponse)
async def get_member_credentials(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> MemberCredentialsResponse:
    """Get or create member credentials (Lazy initialization).

    Migration 034: Zero-knowledge model.
    - Owner can view plaintext secrets (if visible)
    - Others can view metadata only

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        Member credentials (API key + OAuth app)

    Raises:
        HTTPException: If not authorized
    """
    service = MemberCredentialsService(db)

    try:
        credentials = await service.get_or_create_credentials(
            workspace_id=workspace_id,
            user_id=user_id,
            requester_id=user["user_id"],
        )

        # Get target user's workspace role (for permission checks)
        target_role = await service.get_workspace_role(user_id, workspace_id)

        return MemberCredentialsResponse(**credentials, target_user_role=target_role)
    except HTTPException:
        raise
    except ValueError as e:
        logger.error("get_member_credentials_invalid", error=str(e), user_id=user_id)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("get_member_credentials_failed", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.post("/{user_id}/credentials/api-key/hide")
async def hide_api_key(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually hide API key (Owner only).

    Migration 034: Zero-knowledge - only owner can hide.

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        Status message

    Raises:
        HTTPException: If not owner or key not found
    """
    # Permission check: owner only
    if user["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Only owner can hide API key")

    # Get API key (most recent active key)
    from sqlalchemy import and_, select

    from models.auth import APIKey

    result = await db.execute(
        select(APIKey)
        .where(
            and_(
                APIKey.user_id == user_id,
                APIKey.workspace_id == workspace_id,
                APIKey.revoked_at.is_(None),
            )
        )
        .order_by(APIKey.created_at.desc())
        .limit(1)
    )
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    # Hide key
    manager = APIKeyManager(db)
    try:
        await manager.hide_key(api_key.id, user_id)
        await db.commit()
        return {"status": "hidden", "key_id": api_key.id}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post(
    "/{user_id}/credentials/api-key/regenerate",
    response_model=RegenerateAPIKeyResponse,
)
async def regenerate_api_key(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> RegenerateAPIKeyResponse:
    """Regenerate API key (Permission-based).

    Migration 034: Creates new key, revokes old one.

    New permission hierarchy:
    - Self: Always allowed
    - Owner: Can regenerate admin/member/viewer's keys
    - Admin: Can regenerate member/viewer's keys

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        New plaintext key (shown once)

    Raises:
        HTTPException: If not authorized or key not found
    """
    # Permission check: hierarchical
    service = MemberCredentialsService(db)
    await check_permission(service, user["user_id"], workspace_id, user_id, "regenerate")

    from sqlalchemy import and_, select

    from models.auth import APIKey

    # Get old key (most recent active key)
    result = await db.execute(
        select(APIKey)
        .where(
            and_(
                APIKey.user_id == user_id,
                APIKey.workspace_id == workspace_id,
                APIKey.revoked_at.is_(None),
            )
        )
        .order_by(APIKey.created_at.desc())
        .limit(1)
    )
    old_key = result.scalar_one_or_none()

    if not old_key:
        raise HTTPException(status_code=404, detail="API key not found")

    # Revoke old key

    old_key.revoked_at = utcnow()

    # Create new key
    manager = APIKeyManager(db)
    new_plaintext_key = await manager.create_key(
        name=old_key.name,
        user_id=user_id,
        workspace_id=workspace_id,
        auto_hide_minutes=10,
    )

    # Get new key record (most recently created)
    result = await db.execute(
        select(APIKey)
        .where(
            and_(
                APIKey.user_id == user_id,
                APIKey.workspace_id == workspace_id,
                APIKey.revoked_at.is_(None),
            )
        )
        .order_by(APIKey.created_at.desc())
        .limit(1)
    )
    new_key = result.scalar_one()

    await db.commit()

    logger.info(
        "api_key_regenerated",
        old_key_id=old_key.id,
        new_key_id=new_key.id,
        user_id=user_id,
    )

    return RegenerateAPIKeyResponse(
        key=new_plaintext_key,
        key_prefix=new_key.key_prefix,
        key_id=new_key.id,
    )


@router.post(
    "/{user_id}/credentials/api-keys", response_model=MemberAPIKeyResponse, status_code=201
)
async def create_api_key(
    workspace_id: UUID,
    user_id: str,
    data: CreateAPIKeyRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create new API key (Owner only).

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        data: API key creation data (name, auto_hide_minutes)
        user: Current user (from auth)
        db: Database session

    Returns:
        Created API key with plaintext (shown once)

    Raises:
        HTTPException: If not owner or name already exists
    """
    # Permission check: only owner can create their own keys
    if user["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Only owner can create API keys")

    manager = APIKeyManager(db)

    try:
        plaintext_key = await manager.create_key(
            name=data.name,
            user_id=user_id,
            workspace_id=workspace_id,
            auto_hide_minutes=data.auto_hide_minutes,
        )

        # Get the created key
        from sqlalchemy import and_, select

        from models.auth import APIKey

        result = await db.execute(
            select(APIKey)
            .where(
                and_(
                    APIKey.user_id == user_id,
                    APIKey.workspace_id == workspace_id,
                    APIKey.name == data.name,
                    APIKey.revoked_at.is_(None),
                )
            )
            .order_by(APIKey.created_at.desc())
            .limit(1)
        )
        new_key = result.scalar_one()

        await db.commit()

        logger.info("api_key_created", key_id=new_key.id, user_id=user_id, name=data.name)

        # Return with plaintext (shown once)
        return {
            "id": new_key.id,
            "name": new_key.name,
            "key_prefix": new_key.key_prefix,
            "plaintext_key": plaintext_key,
            "is_visible": True,
            "visibility_expires_at": new_key.visibility_expires_at.isoformat()
            if new_key.visibility_expires_at
            else None,
            "created_at": new_key.created_at.isoformat(),
            "revoked_at": None,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_api_key_failed", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.delete("/{user_id}/credentials/api-key")
async def delete_api_key(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete API key (Permission-based).

    Permission matrix:
    - Owner: Delete own key
    - Workspace Owner: Delete any member's key
    - Workspace Admin: Delete member/viewer's key

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        Status message

    Raises:
        HTTPException: If not authorized or key not found
    """
    service = MemberCredentialsService(db)

    # Permission check
    await check_permission(service, user["user_id"], workspace_id, user_id, "delete")

    # Get and delete key
    from sqlalchemy import and_, select

    from models.auth import APIKey

    result = await db.execute(
        select(APIKey)
        .where(
            and_(
                APIKey.user_id == user_id,
                APIKey.workspace_id == workspace_id,
                APIKey.revoked_at.is_(None),  # Only delete active keys
            )
        )
        .order_by(APIKey.created_at.desc())
        .limit(1)
    )
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    await db.delete(api_key)
    await db.commit()

    logger.info("api_key_deleted", key_id=api_key.id, user_id=user_id)

    return {"status": "deleted", "key_id": api_key.id}


# ============================================================================
# OAuth App Endpoints
# ============================================================================


@router.post("/{user_id}/credentials/oauth/hide")
async def hide_oauth_app(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually hide OAuth app secret (Owner only).

    Migration 034: Zero-knowledge - only owner can hide.

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        Status message

    Raises:
        HTTPException: If not owner or app not found
    """
    # Permission check: owner only
    if user["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Only owner can hide OAuth app")

    from sqlalchemy import and_, select

    from models.auth import OAuth2Client

    result = await db.execute(
        select(OAuth2Client).where(
            and_(
                OAuth2Client.owner_id == user_id,
                OAuth2Client.workspace_id == workspace_id,
            )
        )
    )
    oauth_app = result.scalar_one_or_none()

    if not oauth_app:
        raise HTTPException(status_code=404, detail="OAuth app not found")

    # Hide app
    oauth_app.hidden_at = utcnow()
    oauth_app.visibility_expires_at = None  # Cancel auto-hide
    oauth_app.plaintext_secret_encrypted = None  # Migration 035: Delete encrypted secret

    await db.commit()

    logger.info("oauth_app_hidden", client_id=oauth_app.client_id, user_id=user_id)

    return {"status": "hidden", "client_id": oauth_app.client_id}


@router.post(
    "/{user_id}/credentials/oauth/regenerate",
    response_model=RegenerateOAuthSecretResponse,
)
async def regenerate_oauth_secret(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> RegenerateOAuthSecretResponse:
    """Regenerate OAuth client secret (Permission-based).

    Migration 034: Generates new secret, updates hash.

    New permission hierarchy:
    - Self: Always allowed
    - Owner: Can regenerate admin/member/viewer's secrets
    - Admin: Can regenerate member/viewer's secrets

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        New plaintext secret (shown once)

    Raises:
        HTTPException: If not authorized or app not found
    """
    # Permission check: hierarchical
    service = MemberCredentialsService(db)
    can_regenerate = await service.check_can_manage(
        requester_id=user["user_id"],
        workspace_id=workspace_id,
        target_user_id=user_id,
        action="regenerate",
    )

    if not can_regenerate:
        raise HTTPException(status_code=403, detail="Not authorized to regenerate OAuth secret")

    import hashlib
    import secrets
    from datetime import timedelta

    from sqlalchemy import and_, select

    from models.auth import OAuth2Client

    # Get OAuth app
    result = await db.execute(
        select(OAuth2Client).where(
            and_(
                OAuth2Client.owner_id == user_id,
                OAuth2Client.workspace_id == workspace_id,
            )
        )
    )
    oauth_app = result.scalar_one_or_none()

    if not oauth_app:
        raise HTTPException(status_code=404, detail="OAuth app not found")

    # Generate new secret
    new_secret = secrets.token_urlsafe(32)
    new_secret_hash = hashlib.sha256(new_secret.encode()).hexdigest()

    # Migration 035: Encrypt plaintext for storage
    from utils.encryption import get_encryptor

    plaintext_secret_encrypted = get_encryptor().encrypt(new_secret)

    # Update app
    oauth_app.client_secret_hash = new_secret_hash
    oauth_app.hidden_at = None  # Make visible
    oauth_app.visibility_expires_at = utcnow() + timedelta(minutes=10)  # 10 minutes
    oauth_app.plaintext_secret_encrypted = plaintext_secret_encrypted  # Migration 035

    await db.commit()

    logger.info(
        "oauth_secret_regenerated",
        client_id=oauth_app.client_id,
        user_id=user_id,
    )

    return RegenerateOAuthSecretResponse(
        client_secret=new_secret,
        client_id=oauth_app.client_id,
    )


@router.delete("/{user_id}/credentials/oauth")
async def delete_oauth_app(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete OAuth app (Permission-based).

    Permission matrix:
    - Owner: Delete own app
    - Workspace Owner: Delete any member's app
    - Workspace Admin: Delete member/viewer's app

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        Status message

    Raises:
        HTTPException: If not authorized or app not found
    """
    service = MemberCredentialsService(db)

    # Permission check
    can_delete = await service.check_can_manage(
        requester_id=user["user_id"],
        workspace_id=workspace_id,
        target_user_id=user_id,
        action="delete",
    )

    if not can_delete:
        raise HTTPException(status_code=403, detail="Not authorized to delete OAuth app")

    # Get and delete app
    from sqlalchemy import and_, select

    from models.auth import OAuth2Client

    result = await db.execute(
        select(OAuth2Client).where(
            and_(
                OAuth2Client.owner_id == user_id,
                OAuth2Client.workspace_id == workspace_id,
            )
        )
    )
    oauth_app = result.scalar_one_or_none()

    if not oauth_app:
        raise HTTPException(status_code=404, detail="OAuth app not found")

    await db.delete(oauth_app)
    await db.commit()

    logger.info("oauth_app_deleted", client_id=oauth_app.client_id, user_id=user_id)

    return {"status": "deleted", "client_id": oauth_app.client_id}
