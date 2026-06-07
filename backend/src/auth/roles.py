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

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy import func

from config.settings import get_settings
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import ConflictError
from utils.hashing import hmac_sha256_hex
from utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.exc import IntegrityError as _IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    from models.auth import User as _UserModel

logger = get_logger(__name__)


# Sentinel actor identifier for audit_logs.user_email when the OAuth callback
# itself initiates the row (e.g. oauth_user_email_synced). Distinct from the
# admin/user email actor strings so audit_logs.user_email never carries the
# subject's mutable email — the subject is identified by user_id, which is
# pseudonymized at erasure time via _pseudonymize_audit_logs.
_OAUTH_CALLBACK_ACTOR = "oauth-callback"


def _is_email_unique_violation(exc: _IntegrityError) -> bool:
    """True iff ``exc`` is a UNIQUE violation on ``users.email``.

    Asyncpg surfaces UNIQUE violations with sqlstate=23505 and a
    ``constraint_name``. SQLAlchemy 2.0 + asyncpg wraps the native
    ``asyncpg.exceptions.UniqueViolationError`` inside
    ``AsyncAdapt_asyncpg_dbapi.IntegrityError``, so ``exc.orig`` exposes
    ``sqlstate`` but NOT ``constraint_name`` — that field lives on
    ``orig.__cause__`` (the actual asyncpg exception). Earlier versions
    of this function only read ``orig.constraint_name`` and silently
    returned False for every cross-provider email collision, leaking the
    raw IntegrityError out as 503 DB-002 instead of the intended 409
    ConflictError → ``/login?error=email_in_use`` redirect (Issue #481).

    Lookup order:

    1. ``orig.constraint_name`` — direct, in case asyncpg ever surfaces
       it on the wrapper.
    2. ``orig.__cause__.constraint_name`` — the actual native asyncpg
       error, where the constraint name lives in practice today.
    3. Substring match on the formatted message text — defensive
       fallback for SQLAlchemy / asyncpg / driver upgrades that change
       the wrap chain.

    The constraint name is normalised to lower-case and matched on
    ``"email"`` so the alembic baseline's ``ix_users_email`` index name
    AND any future ``users_email_key`` rename both hit. Narrowing to
    ``"email"`` prevents future constraints on other columns (e.g.
    UNIQUE on ``name``) from being mis-mapped to ConflictError("Email
    already in use").
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    if getattr(orig, "sqlstate", None) != "23505":
        return False
    # Try attribute on the wrapper first (rare today, future-proof).
    constraint = (getattr(orig, "constraint_name", "") or "").lower()
    if not constraint:
        # asyncpg wraps the native exception as orig.__cause__; that's
        # where constraint_name actually lives in SQLAlchemy 2.0 today.
        cause = getattr(orig, "__cause__", None)
        if cause is not None:
            constraint = (getattr(cause, "constraint_name", "") or "").lower()
    if "email" in constraint:
        return True
    # Final defensive layer: scan the formatted message. Both asyncpg
    # and SQLAlchemy include ``unique constraint "<name>"`` in str(orig).
    # This catches future wrap-chain changes that drop both attributes.
    message = str(orig).lower() if orig is not None else ""
    return "email" in message and ("unique constraint" in message or "duplicate key" in message)


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
        self,
        email: str,
        user_id: str,
        name: str | None = None,
        auth_provider: str | None = None,
        *,
        email_verified: bool = False,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Role:
        """Ensure user exists; on existing users, sync email/name from IdP.

        Issue #481: Lookup is keyed by ``user_id`` (OAuth ``sub`` claim), not by
        email — ``user_id`` is the immutable identity, ``email`` is mutable
        user-facing data. On hit, when ``email_verified=True`` and the IdP-
        provided email differs from the stored value, the stored email is
        updated and an ``oauth_user_email_synced`` audit row is written with
        HMAC-SHA256 hashes of old/new values. Same-row no-op email writes and
        ``email_verified=False`` paths skip the UPDATE entirely.

        First user is automatically assigned ADMIN role. Subsequent users are
        assigned USER role by default.

        Args:
            email: User email address (from IdP ``user_info["email"]``).
            user_id: User ID (OAuth2 ``sub`` claim) — the lookup key.
            name: User display name (optional). Synced on every login.
            auth_provider: OAuth provider used for registration (e.g.,
                ``"google"``, ``"github"``). Only stored on new user creation;
                not updated on subsequent logins.
            email_verified: Whether the IdP attested that ``email`` is verified.
                Required for the email-sync UPDATE; absent or ``False`` skips
                the email field. Name is always sync-safe regardless.
            ip_address: Caller IP, captured for the audit log row when an email
                change is recorded.
            user_agent: Caller User-Agent, captured for the audit log row.

        Returns:
            The user's assigned role.

        Raises:
            ConflictError: When an email UPDATE would violate the
                ``users.email`` UNIQUE constraint (different account already
                holds the new address). The caller's transaction is rolled
                back; the row's prior state is preserved.

        Example:
            >>> role = await role_manager.ensure_user(
            ...     email="user@example.com",
            ...     user_id="google-123",
            ...     auth_provider="google",
            ...     email_verified=True,
            ... )
        """
        if self.use_postgres:
            return await self._ensure_user_postgres(
                email=email,
                user_id=user_id,
                name=name,
                auth_provider=auth_provider,
                email_verified=email_verified,
                ip_address=ip_address,
                user_agent=user_agent,
            )

        # In-memory backend (testing only) — keyed by email for backward
        # compatibility with existing test suite. Postgres path uses user_id.
        if self._roles is None:
            self._roles = {}

        if email in self._roles:
            return self._roles[email]

        role = Role.ADMIN if len(self._roles) == 0 else Role.USER
        self._roles[email] = role
        return role

    async def _ensure_user_postgres(
        self,
        *,
        email: str,
        user_id: str,
        name: str | None,
        auth_provider: str | None,
        email_verified: bool,
        ip_address: str | None,
        user_agent: str | None,
    ) -> Role:
        """PostgreSQL-backed ``ensure_user``. See ``ensure_user`` docstring."""
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from db.base import get_db
        from models.auth import User, UserOAuthProvider

        # Issue #517: only google/github identities participate in the
        # (provider, oauth_sub) account-linking table. Other auth_provider
        # values (or None) resolve via the legacy user_id path only.
        known_provider = auth_provider in ("google", "github")

        async for db in get_db():
            user = None
            # Provider link for (auth_provider, user_id). Fetched in the new
            # path below and reused by the self-heal block, so it must be in
            # scope even when known_provider is False.
            link = None

            # NEW path (#517): resolve by (provider, oauth_sub) so a secondary
            # provider linked to an existing user maps back to that owner.
            if known_provider:
                link = (
                    await db.execute(
                        select(UserOAuthProvider).filter_by(
                            provider=auth_provider, oauth_sub=user_id
                        )
                    )
                ).scalar_one_or_none()
                if link is not None:
                    user = (
                        await db.execute(select(User).filter_by(user_id=link.user_id))
                    ).scalar_one_or_none()
                    if user is not None:
                        # Touch last_used_at; committed by _sync_existing_user.
                        link.last_used_at = utcnow()

            # LEGACY fallback: user_id == oauth_sub (un-backfilled / pre-#361
            # rows, or non-google/github providers). Guarantees production reads
            # never split while the legacy users.user_id column remains intact.
            if user is None:
                user = (
                    await db.execute(select(User).filter_by(user_id=user_id))
                ).scalar_one_or_none()
                if user is not None and known_provider:
                    # SELF-HEAL: backfill the missing provider row so the next
                    # login resolves via the new path. Reuse the link fetched
                    # in the new path above (same session/transaction, no
                    # intervening write to user_oauth_providers) — guarding on
                    # link is None keeps the insert idempotent. The orphan-link
                    # edge case (link present but its owner User missing) is
                    # correctly skipped here: link is not None, so no duplicate
                    # row is added for the user resolved via the legacy path.
                    if link is None:
                        # Wrap the self-heal insert in a SAVEPOINT: two
                        # concurrent first-logins of the SAME legacy user via the
                        # SAME provider both reach this backfill and race on the
                        # uq_user_oauth_providers_* UNIQUE constraints. Without a
                        # savepoint the loser's IntegrityError would poison the
                        # outer transaction that _sync_existing_user commits.
                        # The concurrent winner already created the row, so a
                        # UNIQUE violation here is benign — swallow and continue;
                        # the legacy user is still resolved via the user_id path.
                        try:
                            async with db.begin_nested():
                                db.add(
                                    UserOAuthProvider(
                                        user_id=user.user_id,
                                        provider=auth_provider,
                                        oauth_sub=user_id,
                                        last_used_at=utcnow(),
                                    )
                                )
                                await db.flush()
                        except IntegrityError:
                            logger.info(
                                "oauth_provider_self_heal_race",
                                user_id=user.user_id,
                                provider=auth_provider,
                            )

            if user is not None:
                return await self._sync_existing_user(
                    db=db,
                    user=user,
                    new_email=email,
                    new_name=name,
                    auth_provider=auth_provider,
                    email_verified=email_verified,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )

            # New user path. Determine role: first user = ADMIN, others = USER.
            count_result = await db.execute(select(func.count()).select_from(User))
            user_count = count_result.scalar() or 0
            role = Role.ADMIN if user_count == 0 else Role.USER

            new_user = User(
                email=email,
                user_id=user_id,
                name=name,
                role=role.value,
                # Issue #166: first admin protected from deletion/demotion.
                is_initial_admin=(role == Role.ADMIN),
                last_login_at=utcnow(),
                auth_provider=auth_provider,
            )
            db.add(new_user)
            # Issue #517: register the provider identity so future logins
            # resolve via the (provider, oauth_sub) path. Same unit of work as
            # the user insert — committed (or rolled back) atomically below.
            if known_provider:
                db.add(
                    UserOAuthProvider(
                        user_id=user_id,
                        provider=auth_provider,
                        oauth_sub=user_id,
                        last_used_at=utcnow(),
                    )
                )
            try:
                await db.commit()
                return role
            except IntegrityError as exc:
                # Two collision shapes share this except:
                #  (a) user_id race: another request just inserted the same
                #      oauth_sub. Re-lookup by user_id and route the existing
                #      row through _sync_existing_user so concurrent
                #      first-logins still get last_login_at updated and any
                #      email/name drift synced (Copilot review #516).
                #  (b) email collision: oauth_sub is novel but email belongs
                #      to a different account (different provider, same
                #      address). The user_id re-lookup misses; raise 409.
                await db.rollback()
                retry = await db.execute(select(User).filter_by(user_id=user_id))
                existing = retry.scalar_one_or_none()
                if existing is not None:
                    return await self._sync_existing_user(
                        db=db,
                        user=existing,
                        new_email=email,
                        new_name=name,
                        auth_provider=auth_provider,
                        email_verified=email_verified,
                        ip_address=ip_address,
                        user_agent=user_agent,
                    )
                if not _is_email_unique_violation(exc):
                    raise
                logger.warning(
                    "oauth_email_collision_attempt",
                    auth_provider=auth_provider,
                    new_email_hmac=hmac_sha256_hex(email, get_settings().audit_hmac_key),
                    user_id=user_id,
                    phase="create",
                )
                raise ConflictError("Email address is already in use by another account") from exc

        # Defensive: get_db() yields exactly once; this is unreachable in
        # practice but satisfies the type checker.
        return Role.USER  # pragma: no cover

    async def _sync_existing_user(
        self,
        *,
        db: AsyncSession,
        user: _UserModel,
        new_email: str,
        new_name: str | None,
        auth_provider: str | None,
        email_verified: bool,
        ip_address: str | None,
        user_agent: str | None,
    ) -> Role:
        """Sync mutable attributes (email, name) on an existing user row.

        Email syncs only when ``email_verified`` is True AND the value
        differs. Name syncs whenever provided and different. UPDATE-collision
        on ``users.email`` UNIQUE raises ``ConflictError`` (rolled back).
        """
        from sqlalchemy.exc import IntegrityError

        from models.auth import AuditLog

        sync_email = email_verified and user.email != new_email
        sync_name = new_name is not None and user.name != new_name

        if not sync_email and not sync_name:
            user.last_login_at = utcnow()
            await db.commit()
            return Role(user.role)

        # Capture user attributes BEFORE the commit attempt. After a failed
        # commit (e.g. UNIQUE violation on email), SQLAlchemy expires the
        # ORM instance and the next attribute access tries to lazy-load
        # from the DB. With async sessions on asyncpg that lazy-load
        # raises ``MissingGreenlet: greenlet_spawn has not been called``
        # because we're already in an exception-handling path with the
        # greenlet context torn down. Reading the values up front into
        # plain locals avoids the lazy-load entirely.
        existing_email = user.email
        user_user_id = user.user_id
        user_role = user.role

        hmac_key = get_settings().audit_hmac_key
        try:
            if sync_email:
                # Audit row written first so a failed commit rolls it back too.
                # user_email is a sentinel actor string, not the subject's
                # email — keeping mutable email out of audit_logs.user_email
                # avoids stranded plaintext after the next email rotation
                # (the subject is recoverable via user_id, which is
                # pseudonymized at erasure time).
                audit = AuditLog(
                    user_email=_OAUTH_CALLBACK_ACTOR,
                    user_id=user_user_id,
                    action="oauth_user_email_synced",
                    resource=f"user:{user_user_id}",
                    old_value_hash=hmac_sha256_hex(existing_email, hmac_key),
                    new_value_hash=hmac_sha256_hex(new_email, hmac_key),
                    user_metadata={"auth_provider": auth_provider},
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                db.add(audit)
                user.email = new_email
            if sync_name:
                user.name = new_name
            user.last_login_at = utcnow()
            await db.commit()
            return Role(user_role)
        except IntegrityError as exc:
            await db.rollback()
            if not _is_email_unique_violation(exc):
                raise
            logger.warning(
                "oauth_email_collision_attempt",
                auth_provider=auth_provider,
                new_email_hmac=hmac_sha256_hex(new_email, hmac_key),
                user_id=user_user_id,
                phase="update",
            )
            raise ConflictError("Email address is already in use by another account") from exc

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
                        assigned_at=to_utc_iso(u.created_at) or "",
                        assigned_by=None,  # TODO: Track who assigned role
                    )
                    for u in db_users
                ]
            # async for over get_db() always yields at least one session
            # in practice, but the type system can't see that — return [] so
            # the function has an explicit list[UserRole] in every code path.
            return []
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
