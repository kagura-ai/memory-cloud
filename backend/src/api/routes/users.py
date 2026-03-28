"""User Profile Management API.

Issue #175: User timezone settings for localized time display
Issue #221: i18n support (locale management)
Issue #252: Session-only authentication (no API keys)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from db.base import get_db
from models.auth import User
from models.schemas import UpdateUserProfileRequest, UserProfileResponse, UserResponse
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserResponse)
async def get_current_user(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Get current user profile.

    Issue #252: Session-only (no API keys).

    Returns:
        Current user information including timezone

    Example:
        GET /api/v1/users/me
        Response: {"email": "...", "timezone": "Asia/Tokyo", ...}
    """
    result = await db.execute(select(User).where(User.user_id == user["user_id"]))
    db_user = result.scalar_one_or_none()

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserResponse.model_validate(db_user)


@router.get("/profile", response_model=UserProfileResponse)
async def get_user_profile(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> UserProfileResponse:
    """Get current user profile.

    Issue #221: User profile with locale support
    Issue #252: Session-only (no API keys)

    Returns:
        User profile including locale, timezone, etc.

    Example:
        GET /api/v1/users/profile
        Response: {"email": "...", "locale": "ja", "timezone": "Asia/Tokyo", ...}
    """
    result = await db.execute(select(User).where(User.user_id == user["user_id"]))
    db_user = result.scalar_one_or_none()

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserProfileResponse.model_validate(db_user)


@router.put("/profile", response_model=UserProfileResponse)
async def update_user_profile(
    data: UpdateUserProfileRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> UserProfileResponse:
    """Update user profile (name, timezone, locale).

    Issue #175: User timezone settings
    Issue #221: i18n support (locale)
    Issue #252: Session-only (no API keys)

    Args:
        data: Profile update data (name, timezone, locale)
        user: Current user (from auth)
        db: Database session

    Returns:
        Updated user profile

    Example:
        PUT /api/v1/users/profile
        Body: {"locale": "ja", "timezone": "Asia/Tokyo"}
        Response: {"email": "...", "locale": "ja", "timezone": "Asia/Tokyo", ...}
    """
    result = await db.execute(select(User).where(User.user_id == user["user_id"]))
    db_user = result.scalar_one_or_none()

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Update fields
    if data.name is not None:
        db_user.name = data.name

    if data.timezone is not None:
        # Security: Input validation to prevent DoS
        if len(data.timezone) > 50:
            raise HTTPException(
                status_code=400, detail="Timezone string too long (max 50 characters)"
            )

        # Validate timezone (Python 3.9+ standard library)
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(data.timezone)  # Validate timezone
            db_user.timezone = data.timezone
        except ZoneInfoNotFoundError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid timezone: {data.timezone}. Must be IANA timezone (e.g., Asia/Tokyo)",
            ) from e

    if data.locale is not None:
        # Locale validation (pattern already enforced in schema)
        db_user.locale = data.locale

    await db.commit()
    await db.refresh(db_user)

    logger.info(
        "user_profile_updated",
        user_id=user["user_id"],
        timezone=db_user.timezone,
        locale=db_user.locale,
    )

    return UserProfileResponse.model_validate(db_user)
