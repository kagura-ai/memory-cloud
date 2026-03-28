"""System Admin Management API Routes.

Issue #166: System Admin vs Workspace Admin RBAC separation.

Endpoints:
- GET /api/v1/admin/system-admins - List all system admins
- POST /api/v1/admin/system-admins - Promote user to system admin
- DELETE /api/v1/admin/system-admins/{user_id} - Demote system admin
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from db.base import get_db
from models.schemas import (
    PromoteToSystemAdminRequest,
    PromoteToSystemAdminResponse,
    SystemAdminListResponse,
    UserWithAdminFlag,
)
from services.system_admin_service import SystemAdminService
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/admin/system-admins", tags=["system-admins"])


async def _get_user_stats(db: AsyncSession, user_id: str) -> int:
    """Get memory count for a user.

    Args:
        db: Database session
        user_id: OAuth2 user_id

    Returns:
        Memory count
    """
    from models.memory import Memory

    # Get memory count
    count_stmt = select(func.count()).select_from(Memory).where(Memory.user_id == user_id)
    count_result = await db.execute(count_stmt)
    memory_count = count_result.scalar() or 0

    return memory_count


@router.get("", response_model=SystemAdminListResponse)
async def list_system_admins(
    current_user: AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """List all system administrators.

    Requires: System Admin role

    Returns:
        List of system admins with is_initial_admin flags and statistics

    Example response:
        {
          "admins": [
            {
              "id": 1,
              "email": "admin@example.com",
              "user_id": "google-oauth2|123456",
              "name": "Admin User",
              "role": "admin",
              "is_initial_admin": true,
              "created_at": "2025-01-01T00:00:00Z",
              "last_login_at": "2025-12-06T10:00:00Z",
              "memory_count": 150,
              "is_active": true
            }
          ],
          "total": 1,
          "initial_admin_id": 1
        }
    """
    service = SystemAdminService(db)
    admins, initial_admin_id = await service.list_system_admins()

    # Convert to response model with stats
    admin_list = []
    for admin in admins:
        # Get memory stats
        memory_count = await _get_user_stats(db, admin.user_id)

        # Determine if active (logged in within last 30 days)
        from datetime import timedelta

        is_active = False
        if admin.last_login_at:
            is_active = (utcnow() - admin.last_login_at) < timedelta(days=30)

        admin_list.append(
            UserWithAdminFlag(
                id=admin.id,
                email=admin.email,
                user_id=admin.user_id,
                name=admin.name or admin.email,
                picture=admin.picture,
                role=admin.role,
                is_initial_admin=admin.is_initial_admin,
                created_at=admin.created_at,
                last_login_at=admin.last_login_at,
                memory_count=memory_count,
                is_active=is_active,
            )
        )

    logger.info(
        "list_system_admins_success",
        requested_by=current_user["email"],
        admin_count=len(admin_list),
        initial_admin_id=initial_admin_id,
    )

    return SystemAdminListResponse(
        admins=admin_list,
        total=len(admin_list),
        initial_admin_id=initial_admin_id,
    )


@router.post("", response_model=PromoteToSystemAdminResponse)
async def promote_to_system_admin(
    request: PromoteToSystemAdminRequest,
    current_user: AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Promote a user to system administrator.

    Requires: System Admin role

    Args:
        request: User ID to promote

    Returns:
        Updated user with success message

    Raises:
        404: User not found
        400: User is already a system admin

    Example request:
        {
          "user_id": "google-oauth2|123456"
        }

    Example response:
        {
          "success": true,
          "user": { ... user details ... },
          "message": "User admin2@example.com promoted to system administrator"
        }
    """
    service = SystemAdminService(db)
    user = await service.promote_to_system_admin(
        user_id=request.user_id, promoted_by=current_user["email"]
    )

    # Get stats
    memory_count = await _get_user_stats(db, user.user_id)

    # Determine if active
    from datetime import timedelta

    is_active = False
    if user.last_login_at:
        is_active = (utcnow() - user.last_login_at) < timedelta(days=30)

    logger.info(
        "promote_to_system_admin_success", user_email=user.email, promoted_by=current_user["email"]
    )

    return PromoteToSystemAdminResponse(
        success=True,
        user=UserWithAdminFlag(
            id=user.id,
            email=user.email,
            user_id=user.user_id,
            name=user.name or user.email,
            picture=user.picture,
            role=user.role,
            is_initial_admin=user.is_initial_admin,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            memory_count=memory_count,
            is_active=is_active,
        ),
        message=f"User {user.email} promoted to system administrator",
    )


@router.delete("/{user_id}")
async def demote_system_admin(
    user_id: str,
    current_user: AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Demote a system administrator to regular user.

    Protection:
    - Cannot demote initial admin (is_initial_admin=True)
    - Cannot demote last remaining admin

    Requires: System Admin role

    Args:
        user_id: User ID to demote

    Returns:
        Success message

    Raises:
        404: User not found
        400: User is not a system admin
        403: Demote operation is protected (initial admin or last admin)

    Example response:
        {
          "success": true,
          "message": "User admin2@example.com demoted to standard user"
        }
    """
    service = SystemAdminService(db)
    user = await service.demote_system_admin(user_id=user_id, demoted_by=current_user["email"])

    logger.info(
        "demote_system_admin_success", user_email=user.email, demoted_by=current_user["email"]
    )

    return {
        "success": True,
        "message": f"User {user.email} demoted to standard user",
    }
