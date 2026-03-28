"""System Administrator Management Service.

Issue #166: System Admin vs Workspace Admin RBAC separation.

This service handles:
- Listing all system admins
- Promoting users to system admin
- Demoting system admins (with protection)
- System admin protection logic

Protection Rules:
- Cannot delete/demote if is_initial_admin=True
- Cannot delete/demote if last remaining admin
"""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import AuditLog, User
from utils.logger import get_logger

logger = get_logger(__name__)


class SystemAdminService:
    """Service for managing system administrators.

    This service implements protection logic to prevent accidental
    lockout from the system by protecting the initial admin and
    ensuring at least one admin always exists.

    Example:
        >>> service = SystemAdminService(db)
        >>> admins, initial_id = await service.list_system_admins()
        >>> await service.promote_to_system_admin("user123", "admin@example.com")
    """

    def __init__(self, db: AsyncSession):
        """Initialize system admin service.

        Args:
            db: Database session
        """
        self.db = db

    async def list_system_admins(self) -> tuple[list[User], int]:
        """List all system administrators.

        Returns:
            Tuple of (admins list, initial_admin_id)

        Example:
            >>> admins, initial_id = await service.list_system_admins()
            >>> for admin in admins:
            ...     print(f"{admin.email} - initial: {admin.is_initial_admin}")
        """
        # Get all admins ordered by creation date
        stmt = select(User).where(User.role == "admin").order_by(User.created_at.asc())
        result = await self.db.execute(stmt)
        admins = list(result.scalars().all())

        # Find initial admin ID
        initial_admin = next((a for a in admins if a.is_initial_admin), None)
        initial_admin_id = initial_admin.id if initial_admin else (admins[0].id if admins else 0)

        logger.info(
            "list_system_admins", admin_count=len(admins), initial_admin_id=initial_admin_id
        )

        return admins, initial_admin_id

    async def promote_to_system_admin(self, user_id: str, promoted_by: str) -> User:
        """Promote user to system administrator.

        Args:
            user_id: OAuth2 user_id to promote
            promoted_by: Email of user performing the promotion

        Returns:
            Updated user object

        Raises:
            HTTPException: 404 if user not found, 400 if already admin

        Example:
            >>> user = await service.promote_to_system_admin(
            ...     "google-oauth2|123456",
            ...     "admin@example.com"
            ... )
            >>> assert user.role == "admin"
        """
        # Get user
        stmt = select(User).where(User.user_id == user_id)
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            logger.warning(
                "promote_to_system_admin_failed", user_id=user_id, reason="user_not_found"
            )
            raise HTTPException(status_code=404, detail="User not found")

        if user.role == "admin":
            logger.warning(
                "promote_to_system_admin_failed", user_email=user.email, reason="already_admin"
            )
            raise HTTPException(status_code=400, detail="User is already a system admin")

        # Update role
        old_role = user.role
        user.role = "admin"
        await self.db.commit()

        # Audit log
        audit = AuditLog(
            user_email=promoted_by,
            user_id=user_id,
            action="system_admin_promote",
            resource=f"user:{user.email}",
            old_value_hash=old_role,
            new_value_hash="admin",
            user_metadata={
                "promoted_by": promoted_by,
                "is_initial_admin": user.is_initial_admin,
            },
        )
        self.db.add(audit)
        await self.db.commit()

        logger.info(
            "system_admin_promoted",
            user_email=user.email,
            promoted_by=promoted_by,
            old_role=old_role,
        )

        return user

    async def demote_system_admin(self, user_id: str, demoted_by: str) -> User:
        """Demote system administrator to regular user.

        Protection Rules:
        - Cannot demote if is_initial_admin=True
        - Cannot demote if last remaining admin

        Args:
            user_id: OAuth2 user_id to demote
            demoted_by: Email of user performing the demotion

        Returns:
            Updated user object

        Raises:
            HTTPException: 404 if not found, 403 if protected

        Example:
            >>> user = await service.demote_system_admin(
            ...     "google-oauth2|123456",
            ...     "admin@example.com"
            ... )
            >>> assert user.role == "user"
        """
        # Get user
        stmt = select(User).where(User.user_id == user_id)
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            logger.warning("demote_system_admin_failed", user_id=user_id, reason="user_not_found")
            raise HTTPException(status_code=404, detail="User not found")

        if user.role != "admin":
            logger.warning("demote_system_admin_failed", user_email=user.email, reason="not_admin")
            raise HTTPException(status_code=400, detail="User is not a system admin")

        # Protection 1: Cannot demote initial admin
        if user.is_initial_admin:
            logger.warning(
                "demote_system_admin_blocked",
                user_email=user.email,
                reason="initial_admin_protected",
            )
            raise HTTPException(
                status_code=403,
                detail="Cannot demote the initial system administrator. This is a protected account.",
            )

        # Protection 2: Cannot demote last remaining admin
        admin_count_stmt = select(func.count()).select_from(User).where(User.role == "admin")
        admin_count_result = await self.db.execute(admin_count_stmt)
        admin_count = admin_count_result.scalar()

        if admin_count <= 1:
            logger.warning(
                "demote_system_admin_blocked",
                user_email=user.email,
                reason="last_admin_protected",
                admin_count=admin_count,
            )
            raise HTTPException(
                status_code=403,
                detail="Cannot demote the last remaining system administrator. At least one admin must exist.",
            )

        # Demote to user
        old_role = user.role
        user.role = "user"
        await self.db.commit()

        # Audit log
        audit = AuditLog(
            user_email=demoted_by,
            user_id=user_id,
            action="system_admin_demote",
            resource=f"user:{user.email}",
            old_value_hash=old_role,
            new_value_hash="user",
            user_metadata={
                "demoted_by": demoted_by,
                "was_initial_admin": user.is_initial_admin,
            },
        )
        self.db.add(audit)
        await self.db.commit()

        logger.warning(
            "system_admin_demoted", user_email=user.email, demoted_by=demoted_by, old_role=old_role
        )

        return user

    async def can_delete_admin(self, user_id: str) -> tuple[bool, str]:
        """Check if system admin can be deleted.

        This method checks both protection rules:
        - is_initial_admin flag
        - Last remaining admin

        Args:
            user_id: OAuth2 user_id to check

        Returns:
            Tuple of (can_delete, reason)
            - (True, "") if deletion is allowed
            - (False, "reason") if deletion is blocked

        Example:
            >>> can_delete, reason = await service.can_delete_admin("user123")
            >>> if not can_delete:
            ...     print(f"Cannot delete: {reason}")
        """
        # Get user
        stmt = select(User).where(User.user_id == user_id)
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or user.role != "admin":
            # Not an admin, deletion is allowed (handled by regular user deletion logic)
            return True, ""

        # Protection 1: Check if initial admin
        if user.is_initial_admin:
            logger.info(
                "delete_admin_blocked", user_email=user.email, reason="initial_admin_protected"
            )
            return False, "Cannot delete the initial system administrator"

        # Protection 2: Check if last admin
        admin_count_stmt = select(func.count()).select_from(User).where(User.role == "admin")
        admin_count_result = await self.db.execute(admin_count_stmt)
        admin_count = admin_count_result.scalar()

        if admin_count <= 1:
            logger.info(
                "delete_admin_blocked",
                user_email=user.email,
                reason="last_admin_protected",
                admin_count=admin_count,
            )
            return False, "Cannot delete the last remaining system administrator"

        # Deletion is allowed
        return True, ""
