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

Both rules are about ``users`` rows with ``role = 'admin'`` and hold in every
``SYSTEM_ADMIN_MODE``. Every role change (``PUT /admin/users/{id}/role`` and
``DELETE /admin/system-admins/{id}``) goes through ``lock_for_role_change``
(#2828), which decides under a row lock.
"""

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import AuditLog, User
from utils.exceptions import AdminProtectionError, BadRequestError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)


def _is_admin_row() -> ColumnElement[bool]:
    """The ``users`` rows that hold the system admin role.

    Written once, for the list, the lock and the counts.
    """
    return User.role == "admin"


class SystemAdminService:
    """Service for managing system administrators.

    This service implements protection logic to prevent accidental
    lockout from the system by protecting the initial admin and
    ensuring at least one admin always exists.

    Example:
        >>> service = SystemAdminService(db)
        >>> admins = await service.list_system_admins()
        >>> await service.promote_to_system_admin("user123", "admin@example.com")
    """

    def __init__(self, db: AsyncSession):
        """Initialize system admin service.

        Args:
            db: Database session
        """
        self.db = db

    async def list_system_admins(self) -> list[User]:
        """List all system administrators, oldest first.

        Returns:
            Admin users; ``is_initial_admin`` marks the protected initial admin.

        Example:
            >>> admins = await service.list_system_admins()
            >>> for admin in admins:
            ...     print(f"{admin.email} - initial: {admin.is_initial_admin}")
        """
        # Get all admins ordered by creation date
        stmt = select(User).where(_is_admin_row()).order_by(User.created_at.asc())
        result = await self.db.execute(stmt)
        admins = list(result.scalars().all())

        logger.info("list_system_admins", admin_count=len(admins))

        return admins

    async def promote_to_system_admin(self, user_id: str, promoted_by: str) -> User:
        """Promote user to system administrator.

        Args:
            user_id: OAuth2 user_id to promote
            promoted_by: Email of user performing the promotion

        Returns:
            Updated user object

        Raises:
            NotFoundException: 404 if user not found.
            BadRequestError: 400 (``REQ-101``) if user is already a system admin.

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
            raise NotFoundException("User")

        if user.role == "admin":
            logger.warning(
                "promote_to_system_admin_failed", user_email=user.email, reason="already_admin"
            )
            raise BadRequestError(
                "User is already a system admin",
                error_code="REQ-101",
            )

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

    async def lock_for_role_change(self, user_id: str, new_role: str) -> User | None:
        """Lock the rows a system-role change depends on, then check it.

        The one check behind ``PUT /admin/users/{id}/role`` and
        ``DELETE /admin/system-admins/{id}`` (#2828). It writes nothing and
        never commits: the caller writes the role and its audit row and
        commits once, while the locks are held.

        The first statement locks every admin row and the target, in
        ``user_id`` order, ``FOR NO KEY UPDATE`` (the strength the role
        ``UPDATE`` takes anyway). Two role changes therefore run one after
        the other, and the second reads what the first committed. The admin
        count is a statement of its own after the lock, so it sees a
        promotion committed while this one waited.

        The rules are about ``role = 'admin'`` rows and read no
        ``SYSTEM_ADMIN_MODE``: an operator in ``service`` mode is held by
        them too.

        Args:
            user_id: ``users.user_id`` of the account whose role changes.
            new_role: The role it is about to get.

        Returns:
            The target row, locked and freshly read, or ``None`` when there
            is no such user (never there, or deleted while this waited).

        Raises:
            AdminProtectionError: 403 ``ADMIN-001`` when the change takes the
                admin role from the initial admin or from the last admin.
        """
        locked = (
            (
                await self.db.execute(
                    select(User)
                    .where(or_(_is_admin_row(), User.user_id == user_id))
                    .order_by(User.user_id)
                    .with_for_update(key_share=True)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        target = next((row for row in locked if row.user_id == user_id), None)
        if target is None:
            return None

        if target.role == "admin" and new_role != "admin":
            admin_count = (
                await self.db.execute(select(func.count()).select_from(User).where(_is_admin_row()))
            ).scalar_one()

            if target.is_initial_admin:
                logger.warning(
                    "demote_system_admin_blocked",
                    user_id=user_id,
                    reason="initial_admin_protected",
                    admin_count=admin_count,
                )
                raise AdminProtectionError(
                    "Cannot demote the initial system administrator. This is a protected account.",
                    reason="initial_admin",
                )

            if admin_count <= 1:
                logger.warning(
                    "demote_system_admin_blocked",
                    user_id=user_id,
                    reason="last_admin_protected",
                    admin_count=admin_count,
                )
                raise AdminProtectionError(
                    "Cannot demote the last remaining system administrator. "
                    "At least one admin must exist.",
                    reason="last_admin",
                )

        return target

    async def demote_system_admin(self, user_id: str, demoted_by: str) -> User:
        """Demote system administrator to regular user.

        Protection Rules (``lock_for_role_change``, shared with the role PUT):
        - Cannot demote if is_initial_admin=True
        - Cannot demote if last remaining admin

        The role change and its audit row are one commit.

        Args:
            user_id: OAuth2 user_id to demote
            demoted_by: Email of user performing the demotion

        Returns:
            Updated user object

        Raises:
            NotFoundException: 404 if user not found.
            BadRequestError: 400 (``REQ-102``) if user is not a system admin.
            AdminProtectionError: 403 if the target is the initial admin or
                the last remaining admin.

        Example:
            >>> user = await service.demote_system_admin(
            ...     "google-oauth2|123456",
            ...     "admin@example.com"
            ... )
            >>> assert user.role == "user"
        """
        # Lock, re-read and check (both protections), before any write.
        user = await self.lock_for_role_change(user_id, "user")

        if not user:
            logger.warning("demote_system_admin_failed", user_id=user_id, reason="user_not_found")
            raise NotFoundException("User")

        if user.role != "admin":
            logger.warning("demote_system_admin_failed", user_email=user.email, reason="not_admin")
            raise BadRequestError(
                "User is not a system admin",
                error_code="REQ-102",
            )

        # Demote to user; the audit row rides the same commit.
        old_role = user.role
        user.role = "user"
        self.db.add(
            AuditLog(
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
        )
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
        admin_count_stmt = select(func.count()).select_from(User).where(_is_admin_row())
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
