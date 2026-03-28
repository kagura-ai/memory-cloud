"""Role-based access control for Kagura Memory Cloud.

Issue #650 - OAuth2 Web Login & API Key Management
Issue #653 - PostgreSQL backend for roles and audit logs

This module provides role definitions and management for user authorization.
Roles determine what actions users can perform in the system.

Example:
    >>> from auth.roles import Role, RoleManager
    >>> role_manager = RoleManager(db_url="postgresql://...")
    >>> role_manager.assign_role("user@example.com", Role.ADMIN)
    >>> role_manager.has_role("user@example.com", Role.ADMIN)
    True
"""

from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import func

from utils.datetime import utcnow


class Role(StrEnum):
    """User roles for access control.

    Attributes:
        ADMIN: Full system access (config, users, all APIs)
        USER: Standard user access (memory APIs, own API keys)
        READ_ONLY: Read-only access (view only, no modifications)
    """

    ADMIN = "admin"
    USER = "user"
    READ_ONLY = "read_only"

    def __str__(self) -> str:
        """String representation."""
        return self.value

    @property
    def display_name(self) -> str:
        """Human-readable display name."""
        return {
            Role.ADMIN: "Administrator",
            Role.USER: "User",
            Role.READ_ONLY: "Read-Only",
        }[self]

    @property
    def description(self) -> str:
        """Role description."""
        return {
            Role.ADMIN: "Full system access including configuration and user management",
            Role.USER: "Standard user access to memory APIs and API keys",
            Role.READ_ONLY: "Read-only access to own data",
        }[self]


class UserRole(BaseModel):
    """User role assignment.

    Attributes:
        email: User email address (from OAuth2)
        user_id: User ID (Google sub claim)
        role: Assigned role
        assigned_at: Role assignment timestamp
        assigned_by: Email of user who assigned this role (for audit)
    """

    email: str = Field(..., description="User email address")
    user_id: str = Field(..., description="User ID (OAuth2 sub)")
    role: Role = Field(default=Role.USER, description="Assigned role")
    assigned_at: str = Field(..., description="ISO 8601 timestamp")
    assigned_by: str | None = Field(None, description="Email of user who assigned this role")


class RoleManager:
    """Manage user roles and permissions (async).

    Uses PostgreSQL for persistent storage of user roles via async sessions.
    First user to log in is automatically assigned ADMIN role.

    Note:
        Issue #38: Refactored to use async/await with db.base.get_db()
        All methods are now async and use PostgreSQL persistence by default.

    Example:
        >>> role_manager = RoleManager(use_postgres=True)
        >>> # First login - auto ADMIN
        >>> await role_manager.ensure_user("user1@example.com", "google-sub-123")
        >>> await role_manager.get_role("user1@example.com")
        <Role.ADMIN: 'admin'>
        >>>
        >>> # Second login - default USER
        >>> await role_manager.ensure_user("user2@example.com", "google-sub-456")
        >>> await role_manager.get_role("user2@example.com")
        <Role.USER: 'user'>
        >>>
        >>> # Check permissions
        >>> await role_manager.has_role("user1@example.com", Role.ADMIN)
        True
    """

    def __init__(self, use_postgres: bool = True):
        """Initialize role manager.

        Args:
            use_postgres: Use PostgreSQL backend (default: True)
                         If False, use in-memory dict (for testing)

        Note:
            Issue #38: Async-ified to use db.base.get_db() for PostgreSQL persistence
            Database URL is managed by db.base module, not passed here.
        """
        self.use_postgres = use_postgres

        if not use_postgres:
            # In-memory backend (fallback)
            self._roles = {}
        else:
            self._roles = None  # Not used in PostgreSQL mode

    async def ensure_user(
        self, email: str, user_id: str, name: str | None = None, auth_provider: str | None = None
    ) -> Role:
        """Ensure user exists, create if first user or new.

        First user is automatically assigned ADMIN role.
        Subsequent users are assigned USER role by default.

        Args:
            email: User email address
            user_id: User ID (OAuth2 sub claim)
            name: User display name (optional)
            auth_provider: OAuth provider used for registration (e.g., "google", "github").
                          Only stored on new user creation; not updated on subsequent logins.

        Returns:
            Assigned role

        Example:
            >>> role = await role_manager.ensure_user("user@example.com", "google-123", auth_provider="google")
        """
        if self.use_postgres:
            # PostgreSQL backend (async)
            from sqlalchemy import select
            from sqlalchemy.exc import IntegrityError

            from db.base import get_db
            from models.auth import User

            async for db in get_db():
                try:
                    # Check if user exists
                    result = await db.execute(select(User).filter_by(email=email))
                    user = result.scalar_one_or_none()

                    if user:
                        # Update last_login_at
                        user.last_login_at = utcnow()
                        await db.commit()
                        return Role(user.role)

                    # Determine role: first user = ADMIN, others = USER
                    count_result = await db.execute(select(func.count()).select_from(User))
                    user_count = count_result.scalar()
                    role = Role.ADMIN if user_count == 0 else Role.USER

                    # Create new user
                    # Issue #166: Mark first admin as initial admin (protected from deletion/demotion)
                    new_user = User(
                        email=email,
                        user_id=user_id,
                        name=name,
                        role=role.value,
                        is_initial_admin=(role == Role.ADMIN and user_count == 0),
                        last_login_at=utcnow(),
                        auth_provider=auth_provider,
                    )
                    db.add(new_user)
                    await db.commit()

                    return role

                except IntegrityError:
                    # Race condition: user created by another request
                    await db.rollback()
                    result = await db.execute(select(User).filter_by(email=email))
                    user = result.scalar_one_or_none()
                    return Role(user.role) if user else Role.USER

        else:
            # In-memory backend
            if self._roles is None:
                self._roles = {}

            if email in self._roles:
                return self._roles[email]

            role = Role.ADMIN if len(self._roles) == 0 else Role.USER
            self._roles[email] = role
            return role

    async def get_role(self, email: str) -> Role | None:
        """Get user's role.

        Args:
            email: User email address

        Returns:
            User's role or None if user not found

        Example:
            >>> role = await role_manager.get_role("user@example.com")
        """
        if self.use_postgres:
            from sqlalchemy import select

            from db.base import get_db
            from models.auth import User

            async for db in get_db():
                result = await db.execute(select(User).filter_by(email=email))
                user = result.scalar_one_or_none()
                return Role(user.role) if user else None
        else:
            if self._roles is None:
                self._roles = {}
            return self._roles.get(email)

    async def has_role(self, email: str, required_role: Role) -> bool:
        """Check if user has required role or higher.

        Role hierarchy: ADMIN > USER > READ_ONLY

        Args:
            email: User email address
            required_role: Required role level

        Returns:
            True if user has required role or higher

        Example:
            >>> await role_manager.has_role("admin@example.com", Role.ADMIN)
            True
            >>> await role_manager.has_role("user@example.com", Role.ADMIN)
            False
        """
        user_role = await self.get_role(email)
        if not user_role:
            return False

        # Role hierarchy
        role_levels = {Role.ADMIN: 3, Role.USER: 2, Role.READ_ONLY: 1}

        return role_levels.get(user_role, 0) >= role_levels.get(required_role, 0)

    async def assign_role(self, email: str, role: Role, assigned_by: str | None = None) -> None:
        """Assign role to user.

        Args:
            email: User email address
            role: Role to assign
            assigned_by: Email of user assigning the role (for audit)

        Raises:
            ValueError: If user not found

        Example:
            >>> await role_manager.assign_role("user@example.com", Role.ADMIN)
        """
        if self.use_postgres:
            from sqlalchemy import select

            from db.base import get_db
            from models.auth import AuditLog, User

            async for db in get_db():
                result = await db.execute(select(User).filter_by(email=email))
                user = result.scalar_one_or_none()
                if not user:
                    raise ValueError(f"User {email} not found")

                old_role = user.role
                user.role = role.value
                user.updated_at = utcnow()
                await db.commit()

                # Log to audit_logs
                audit = AuditLog(
                    user_email=assigned_by or "system",
                    user_id=user.user_id,
                    action="role_assign",
                    resource=f"user:{email}",
                    old_value_hash=old_role,
                    new_value_hash=role.value,
                    user_metadata={"assigned_by": assigned_by},
                )
                db.add(audit)
                await db.commit()
                return

        else:
            if self._roles is None:
                self._roles = {}
            if email not in self._roles:
                raise ValueError(f"User {email} not found")
            self._roles[email] = role

    async def list_users(self) -> list[UserRole]:
        """List all users and their roles.

        Returns:
            List of UserRole objects

        Example:
            >>> users = await role_manager.list_users()
        """
        if self.use_postgres:
            from sqlalchemy import select

            from db.base import get_db
            from models.auth import User

            async for db in get_db():
                result = await db.execute(select(User).order_by(User.created_at))
                db_users = result.scalars().all()
                return [
                    UserRole(
                        email=u.email,
                        user_id=u.user_id,
                        role=Role(u.role),
                        assigned_at=u.created_at.isoformat() if u.created_at else "",
                        assigned_by=None,  # TODO: Track who assigned role
                    )
                    for u in db_users
                ]
        else:
            return []

    async def is_admin(self, email: str) -> bool:
        """Check if user is admin.

        Args:
            email: User email address

        Returns:
            True if user has ADMIN role
        """
        return await self.has_role(email, Role.ADMIN)


# Global singleton instance (will be initialized with DB URL from config)
_role_manager: RoleManager | None = None


def get_role_manager() -> RoleManager:
    """Get global RoleManager instance.

    Returns:
        Global RoleManager singleton

    Raises:
        RuntimeError: If role manager not initialized

    Example:
        >>> from auth.roles import get_role_manager
        >>> role_manager = get_role_manager()
    """
    if _role_manager is None:
        raise RuntimeError(
            "RoleManager not initialized. Call initialize_role_manager(db_url) first."
        )
    return _role_manager


def initialize_role_manager(use_postgres: bool = True) -> RoleManager:
    """Initialize global RoleManager.

    Args:
        use_postgres: Use PostgreSQL backend (default: True)

    Returns:
        Initialized RoleManager instance

    Example:
        >>> from auth.roles import initialize_role_manager
        >>> role_manager = initialize_role_manager(use_postgres=True)
    """
    global _role_manager
    _role_manager = RoleManager(use_postgres=use_postgres)
    return _role_manager
