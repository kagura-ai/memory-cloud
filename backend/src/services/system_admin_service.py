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
(#2828), and every admin-started deletion of an account (``DELETE
/admin/users/{id}``, ``POST /admin/users/{id}/erase``) through
``can_delete_admin`` (#2855). Both decide under the same row lock: the admin
rows and the target, in ``user_id`` order, ``FOR NO KEY UPDATE``, with the
admin count read after it.

An admin whose account has an open erasure request (``ACTIVE_STATUSES``,
self-service or admin-started) does not count as a remaining admin, and its
row is not part of the lock (#2855): an admin erasure decides under the lock
in the transaction that inserts its request, and from that commit on every
other decision counts the account as gone, while the run holds no admin row.
A request that ends ``failed`` or ``cancelled`` makes the account count again.
"""

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import AuditLog, User
from models.erasure import ACTIVE_STATUSES, ErasureRequest
from utils.exceptions import AdminProtectionError, BadRequestError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)


def _is_admin_row() -> ColumnElement[bool]:
    """The ``users`` rows that hold the system admin role.

    Written once, for the list, the lock and the counts.
    """
    return User.role == "admin"


def _counted_admin_row() -> ColumnElement[bool]:
    """The admin rows that count as remaining admins: no open erasure request.

    An account being erased (any request in ``ACTIVE_STATUSES``) counts as
    gone from its request's commit on (#2855).
    """
    under_erasure = exists().where(
        ErasureRequest.user_id == User.user_id,
        ErasureRequest.status.in_(ACTIVE_STATUSES),
    )
    return and_(_is_admin_row(), ~under_erasure)


async def _admin_count(db: AsyncSession, user_id: str) -> int:
    """The admins left to count for a decision about ``user_id``.

    The target itself (when it is an admin) and every other admin that
    counts (``_counted_admin_row``). A statement of its own, run after the
    lock, so it reads what committed while the lock waited.
    """
    return (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(or_(_counted_admin_row(), and_(_is_admin_row(), User.user_id == user_id)))
        )
    ).scalar_one()


def _admin_rows_lock(user_id: str, also_lock: ColumnElement[bool] | None = None) -> Select:
    """The one statement that locks what a system-role decision reads.

    Every admin row that counts (``_counted_admin_row``: not one under
    erasure, whose run holds it), the target and the rows ``also_lock``
    matches, in ``user_id`` order, ``FOR NO KEY UPDATE`` (never ``FOR
    UPDATE``: a ``FOR KEY SHARE`` holder of another account's row does not
    wait on it, and nothing here waits on one). ``populate_existing``: a row
    the session already holds is refreshed from the locked read.
    """
    conditions = [_counted_admin_row(), User.user_id == user_id]
    if also_lock is not None:
        conditions.append(also_lock)
    return (
        select(User)
        .where(or_(*conditions))
        .order_by(User.user_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )


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

        The first statement locks every admin row that counts and the
        target, in ``user_id`` order, ``FOR NO KEY UPDATE`` (the strength the
        role ``UPDATE`` takes anyway). Two role changes therefore run one
        after the other, and the second reads what the first committed. The
        admin count is a statement of its own after the lock, so it sees a
        promotion committed while this one waited. An admin with an open
        erasure request is neither locked nor counted (#2855).

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
        locked = (await self.db.execute(_admin_rows_lock(user_id))).scalars().all()
        target = next((row for row in locked if row.user_id == user_id), None)
        if target is None:
            return None

        if target.role == "admin" and new_role != "admin":
            admin_count = await _admin_count(self.db, user_id)

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

    async def can_delete_admin(
        self, user_id: str, *, also_lock: ColumnElement[bool] | None = None
    ) -> tuple[bool, str]:
        """Check if system admin can be deleted, under the role-change lock.

        This method checks both protection rules:
        - is_initial_admin flag
        - Last remaining admin

        It decides under the lock of ``lock_for_role_change`` (#2855): its
        first statement locks every admin row that counts and the target, in
        ``user_id`` order, ``FOR NO KEY UPDATE``, and the admin count is a
        statement of its own after it (a promotion committed while this
        waited counts; an admin with an open erasure request does not). The
        lock is taken for any target, admin or not, and is held until the
        caller's transaction ends: the caller deletes the account, or inserts
        its erasure request, in that transaction, so a deletion and a role
        change, or two deletions, run one after the other. It writes nothing
        and never commits.

        A transaction's first lock statement must take every ``users`` row it
        locks later (the lock order of
        ``tests/integration/test_erasure_lock_order_db.py``): ``also_lock``
        names those rows, so a later statement only strengthens a row the
        transaction already holds.

        Args:
            user_id: OAuth2 user_id to check
            also_lock: The other ``users`` rows the caller's transaction
                locks later, locked in the same statement.

        Returns:
            Tuple of (can_delete, reason)
            - (True, "") if deletion is allowed (also for a non-admin or an
              unknown id)
            - (False, "reason") if deletion is blocked

        Example:
            >>> can_delete, reason = await service.can_delete_admin("user123")
            >>> if not can_delete:
            ...     print(f"Cannot delete: {reason}")
        """
        locked = (await self.db.execute(_admin_rows_lock(user_id, also_lock))).scalars().all()
        user = next((row for row in locked if row.user_id == user_id), None)

        if not user or user.role != "admin":
            # Not an admin, deletion is allowed (handled by regular user deletion logic)
            return True, ""

        # Protection 1: Check if initial admin
        if user.is_initial_admin:
            logger.info(
                "delete_admin_blocked", user_email=user.email, reason="initial_admin_protected"
            )
            return False, "Cannot delete the initial system administrator"

        # Protection 2: Check if last admin (counted after the lock)
        admin_count = await _admin_count(self.db, user_id)

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
