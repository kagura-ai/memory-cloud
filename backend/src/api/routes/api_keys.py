"""API Key management routes.

Provides CRUD operations for API keys with role-based access control.
Issue #106: Refactored to use consolidated utilities
Issue #252: Session-only authentication (no API keys)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import APIKeyManager
from auth.dependencies import SessionUser, get_current_user
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import APIKey
from utils import db_transaction, get_user_id
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/config/api-keys", tags=["api-keys"])


# ============================================================================
# Dependency Injection
# ============================================================================


async def get_api_key_manager(db: AsyncSession = Depends(get_db)) -> APIKeyManager:
    """Get APIKeyManager instance.

    Args:
        db: Database session

    Returns:
        APIKeyManager instance
    """
    return APIKeyManager(db)


# ============================================================================
# Pydantic Models
# ============================================================================


class APIKeyCreate(BaseModel):
    """Request model for creating an API key."""

    name: str = Field(
        ..., min_length=1, max_length=100, description="Friendly name for the API key"
    )
    expires_days: int | None = Field(
        None,
        ge=1,
        le=3650,
        description="Expiration in days (30, 90, 365, or None for no expiration)",
    )


class DailyStats(BaseModel):
    """Daily usage statistics."""

    date: str = Field(..., description="Date in ISO format (YYYY-MM-DD)")
    count: int = Field(..., ge=0, description="Number of requests on this date")


class APIKeyStats(BaseModel):
    """API key usage statistics."""

    total_requests: int = Field(..., ge=0, description="Total requests in period")
    daily_stats: list[DailyStats] = Field(..., description="Daily breakdown")
    period_start: str = Field(..., description="Start date of statistics period")
    period_end: str = Field(..., description="End date of statistics period")


class APIKeyResponse(TZAwareBaseModel):
    """Response model for API key metadata."""

    id: int = Field(..., description="Database ID")
    key_prefix: str = Field(..., description="First 16 characters of key (for display)")
    name: str = Field(..., description="Friendly name")
    user_id: str = Field(..., description="Owner user ID")
    created_at: datetime = Field(..., description="Creation timestamp")
    last_used_at: datetime | None = Field(None, description="Last usage timestamp")
    revoked_at: datetime | None = Field(None, description="Revocation timestamp")
    expires_at: datetime | None = Field(None, description="Expiration timestamp")
    status: Literal["active", "revoked", "expired"] = Field(..., description="Current status")

    model_config = {"from_attributes": True}


class APIKeyCreateResponse(APIKeyResponse):
    """Response model for API key creation (includes plaintext key)."""

    api_key: str = Field(
        ...,
        description="Plaintext API key (ONLY shown once - must be saved by client)",
    )


# ============================================================================
# Helper Functions
# ============================================================================


def _determine_status(
    revoked_at: datetime | None, expires_at: datetime | None
) -> Literal["active", "revoked", "expired"]:
    """Determine API key status.

    Args:
        revoked_at: Revocation timestamp
        expires_at: Expiration timestamp

    Returns:
        Status string: "active", "revoked", or "expired"
    """
    if revoked_at:
        return "revoked"

    if expires_at:
        if utcnow() > expires_at:
            return "expired"

    return "active"


def _format_key_response(key: APIKey) -> APIKeyResponse:
    """Format APIKey object into APIKeyResponse.

    Args:
        key: APIKey ORM object

    Returns:
        Formatted APIKeyResponse model
    """
    status = _determine_status(key.revoked_at, key.expires_at)

    return APIKeyResponse(
        id=key.id,
        key_prefix=key.key_prefix,
        name=key.name,
        user_id=key.user_id,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        expires_at=key.expires_at,
        status=status,
    )


# ============================================================================
# Routes
# ============================================================================


@router.get("", response_model=list[APIKeyResponse])
async def list_api_keys(
    user: SessionUser,
    manager: APIKeyManager = Depends(get_api_key_manager),
) -> list[APIKeyResponse]:
    """List API keys for the current user.

    Issue #246: Returns all user's API keys (no context filtering).
    """
    try:
        user_id = get_user_id(user)
        # Issue #246: current_context_id removed
        # current_context_id = user.get("current_context_id")

        keys = await manager.list_keys(
            user_id=user_id,
        )
        return [_format_key_response(key) for key in keys]
    except Exception as e:
        logger.error(f"Failed to list API keys: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve API keys",
        ) from e


@router.post("", response_model=APIKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    data: APIKeyCreate,
    user: SessionUser,
    manager: APIKeyManager = Depends(get_api_key_manager),
) -> APIKeyCreateResponse:
    """Create a new API key for the current user.

    Issue #246: Creates API key with context_id=None (no auto-assignment).
    """
    try:
        user_id = get_user_id(user)
        # Issue #246: current_context_id removed
        # current_context_id = user.get("current_context_id")

        api_key, created_key = await manager.create_key(
            name=data.name,
            user_id=user_id,
            expires_days=data.expires_days,
        )

        response_data = _format_key_response(created_key)
        return APIKeyCreateResponse(**response_data.model_dump(), api_key=api_key)

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create API key: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create API key",
        ) from e


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_api_key(
    key_id: int,
    user: SessionUser,
    manager: APIKeyManager = Depends(get_api_key_manager),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete an API key.

    Issue #112: Delete by ID directly to avoid ambiguity with duplicate names across contexts.

    WARNING: Hard delete. Use POST /{key_id}/revoke for soft delete.
    """
    try:
        user_id = get_user_id(user)

        # Delete by ID directly (Issue #112: avoids name ambiguity)
        result = await db.execute(
            select(APIKey).where(
                and_(
                    APIKey.id == key_id,
                    APIKey.user_id == user_id,  # Security: verify ownership
                )
            )
        )
        target_key = result.scalar_one_or_none()

        if not target_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found or not owned by you",
            )

        await db.delete(target_key)
        await db.commit()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete API key: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete API key",
        ) from e


@router.post("/{key_id}/revoke", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoke_api_key(
    key_id: int,
    user: SessionUser,
    manager: APIKeyManager = Depends(get_api_key_manager),
    db: AsyncSession = Depends(get_db),
):
    """Revoke an API key (soft delete, preserves audit trail).

    Issue #112: Revoke by ID directly to avoid ambiguity with duplicate names.
    """
    try:
        user_id = get_user_id(user)

        # Get key by ID (Issue #112: avoids name ambiguity)
        result = await db.execute(
            select(APIKey).where(
                and_(
                    APIKey.id == key_id,
                    APIKey.user_id == user_id,  # Security: verify ownership
                )
            )
        )
        target_key = result.scalar_one_or_none()

        if not target_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found or not owned by you",
            )

        # Revoke the key
        target_key.revoked_at = utcnow()
        await db.commit()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to revoke API key: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke API key",
        ) from e


@router.post("/{key_id}/regenerate", response_model=APIKeyCreateResponse)
async def regenerate_api_key(
    key_id: int,
    user: SessionUser,
    manager: APIKeyManager = Depends(get_api_key_manager),
    db: AsyncSession = Depends(get_db),
) -> APIKeyCreateResponse:
    """Regenerate an API key (revokes old key, creates new one with same name).

    Issue #169: Secret regeneration feature.

    WARNING: This immediately invalidates the old key. Update all applications.
    """
    try:
        user_id = get_user_id(user)

        # Get existing key
        result = await db.execute(
            select(APIKey).where(
                and_(
                    APIKey.id == key_id,
                    APIKey.user_id == user_id,
                )
            )
        )
        old_key = result.scalar_one_or_none()

        if not old_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found or not owned by you",
            )

        if old_key.revoked_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot regenerate a revoked key",
            )

        # Store old key info
        key_name = old_key.name
        key_workspace_id = old_key.workspace_id
        key_expires_at = old_key.expires_at

        # Calculate remaining expiration days if applicable
        expires_days = None
        if key_expires_at:
            remaining = key_expires_at - utcnow()
            if remaining.total_seconds() > 0:
                expires_days = max(1, remaining.days)

        # Revoke old key
        old_key.revoked_at = utcnow()
        await db.flush()

        # Create new key with same name
        new_api_key, new_key = await manager.create_key(
            name=key_name,
            user_id=user_id,
            expires_days=expires_days,
            workspace_id=key_workspace_id,
        )

        await db.commit()

        logger.info(f"api_key_regenerated: old_id={key_id}, new_id={new_key.id}, user={user_id}")

        response_data = _format_key_response(new_key)
        return APIKeyCreateResponse(**response_data.model_dump(), api_key=new_api_key)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to regenerate API key: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to regenerate API key",
        ) from e


@router.get("/{key_id}/stats", response_model=APIKeyStats)
async def get_api_key_stats(
    key_id: int,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
):
    """Get API key usage statistics."""
    user_id = get_user_id(user)

    async with db_transaction(db, "get_api_key_stats", "Failed to retrieve API key statistics"):
        result = await db.execute(
            select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user_id)
        )
        api_key = result.scalar_one_or_none()

        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found or not owned by you",
            )

        # Calculate period
        now = utcnow()
        period_start = now - timedelta(days=days)

        # Generate daily stats (placeholder - TODO: actual usage tracking)
        daily_stats = []
        current_date = period_start.date()
        end_date = now.date()

        while current_date <= end_date:
            daily_stats.append({"date": current_date.isoformat(), "count": 0})
            current_date += timedelta(days=1)

        logger.info(f"api_key_stats_retrieved: key_id={key_id}, days={days}")

        return APIKeyStats(
            total_requests=0,
            daily_stats=daily_stats,
            period_start=period_start.date().isoformat(),
            period_end=end_date.isoformat(),
        )
