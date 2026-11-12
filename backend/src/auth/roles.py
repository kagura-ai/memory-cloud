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

import hashlib
import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import func

from auth.provider_link import provider_link_established
from auth.system_admin_mode import session_admin_enabled
from config.settings import get_settings
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import (
    ConflictError,
    ProviderLinkConfirmable,
    UnlinkedProviderSignInError,
)
from utils.hashing import hmac_sha256_hex
from utils.logger import get_logger
from utils.user_prefs import is_valid_locale, is_valid_timezone

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement
    from sqlalchemy.exc import IntegrityError as _IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    from models.auth import User as _UserModel
    from models.auth import UserOAuthProvider as _UserOAuthProviderModel
    from services.announcement_consent_service import SignupAnnouncementConsent

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


def _adopt_primary_provider(
    user: _UserModel, auth_provider: str | None, link: _UserOAuthProviderModel
) -> None:
    """Make the signing-in provider the primary one of an OAuth account that
    has none (#1875) — when it is the identity the account was created with
    and its link to the account is established.

    ``users.auth_provider`` decides whether a sign-in may sync email and name
    (#1811). It is NULL on a legacy row, and after ``AccountLinkingService``
    removed the account's last provider; nothing else repopulated it, so such
    an account never synced again whichever provider it signed in with.

    ``link`` is this identity's own ``user_oauth_providers`` row for ``user``.
    Only the account's original identity — the one whose sub is the account's
    ``user_id`` (``ensure_user`` creates the account under the first sub, and
    the #517 backfill linked that sub) — can become primary, and only through
    an established link: a row older than the identity-link window, or
    written with the account (:func:`auth.provider_link.provider_link_established`,
    the rule an identity-link proof follows). Any other identity was attached
    later through ``link-provider``, which needs fresh proof of the account
    (the current password, a passkey or a recent verified sign-in), not of
    the attached identity: it signs in as a linked provider does, however old
    its link, so attaching can never make a provider the one whose email and
    name are synced onto the account. A younger link of the original identity signs in the
    same way: no adoption, no profile sync.
    Staged only: ``_sync_existing_user`` commits it.

    A password account (``auth_method == "password"``) that links a provider
    keeps its NULL pointer — its email and name are its own, not a provider's.
    """
    if (
        auth_provider not in ("google", "github")
        or user.auth_provider is not None
        or user.auth_method != "oauth"
    ):
        return
    if link.oauth_sub != user.user_id:
        logger.info(
            "oauth_primary_provider_not_adopted_attached_identity",
            auth_provider=auth_provider,
            user_id=user.user_id,
        )
        return
    if not provider_link_established(
        sub_is_user_id=True,
        linked_at=link.linked_at,
        account_created_at=user.created_at,
    ):
        logger.info(
            "oauth_primary_provider_not_adopted_recent_link",
            auth_provider=auth_provider,
            user_id=user.user_id,
        )
        return
    user.auth_provider = auth_provider
    logger.info("oauth_primary_provider_adopted", auth_provider=auth_provider, user_id=user.user_id)


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


async def new_account_role(db: AsyncSession) -> Role:
    """The role of an account being created now (#2767, #1734).

    The one place for the first-user rule: the first account on an empty
    users table becomes ``admin`` only while this process serves the
    system-admin surface to sessions (``SYSTEM_ADMIN_MODE=session``); in
    ``off`` and ``service`` every new account is a ``user``. Exactly one
    ``db.execute`` (the count), so callers' statement sequences stay stable.
    """
    from sqlalchemy import select

    from models.auth import User

    count_result = await db.execute(select(func.count()).select_from(User))
    user_count = count_result.scalar() or 0
    if user_count == 0 and session_admin_enabled():
        return Role.ADMIN
    return Role.USER


def is_initial_admin_role(role: Role) -> bool:
    """``users.is_initial_admin`` for a new account with the role it was given.

    A bootstrap marker (Issue #166: the first admin is protected from deletion
    and demotion), not a grant of system-admin power: it is true exactly when
    :func:`new_account_role` chose ``admin``, which happens only in
    ``SYSTEM_ADMIN_MODE=session``. The one comparison every account-creating
    path shares (OAuth ``ensure_user`` and email sign-up).
    """
    return role == Role.ADMIN


def _normalize_address(email: str) -> str:
    """``lower(trim())``: the form email sign-up stores (``normalize_email``)."""
    return email.strip().lower()


async def lock_address(db: AsyncSession, email: str) -> None:
    """Serialize account creation for one address until the transaction ends (#1734).

    ``users.email`` is UNIQUE but case-sensitive, so two creations whose
    addresses differ only in case would both pass it. Every path that may
    create an account for an address takes this transaction-scoped advisory
    lock first and then looks the address up case-insensitively: the email
    sign-up request and completion, and ``ensure_user``'s new-user path.
    """
    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('email_signup:' || :address, 0))"),
        {"address": _normalize_address(email)},
    )


async def _address_held_by_another(db: AsyncSession, email: str, user_id: str) -> bool:
    """Does an account other than ``user_id`` hold ``email`` in any letter case? (#1734)

    Takes :func:`lock_address` first, so an account being created for the
    address at the same moment (email sign-up, a first OAuth sign-in) is
    either seen here or sees this account's new address. Two statements:
    the lock and the lookup.
    """
    from sqlalchemy import select

    from models.auth import User

    await lock_address(db, email)
    result = await db.execute(
        select(User.user_id)
        .where(func.lower(User.email) == _normalize_address(email), User.user_id != user_id)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def email_signup_account_clause(email: str) -> ColumnElement[bool]:
    """The account created by email sign-up for ``email`` (#1734).

    Email sign-up stores the normalized address, so plain equality on the
    unique column finds it. It is the only kind of account with
    ``auth_method='password'`` and no login id (every CLI account has one),
    and its address was verified by the sign-up link.
    """
    from sqlalchemy import and_

    from models.auth import User

    return and_(
        User.email == _normalize_address(email),
        User.auth_method == "password",
        User.login_id.is_(None),
        User.email_verified_at.is_not(None),
    )


def email_account_link_refusal(user: _UserModel, *, address: str) -> str | None:
    """Why a provider identity may not be connected to ``user`` now, or None.

    The row-only conditions shared by the automatic link (#1734) and the link
    confirmed by email (#2786), read under the account's row lock in both:

    - ``not_an_email_signup_account``: not the account email sign-up created
      for ``address`` (normalized here; the same fields as
      :func:`email_signup_account_clause`);
    - ``totp_enabled``: two-step verification is on (an OAuth sign-in never
      asks for it);
    - ``role``: anything but an ordinary ``user`` (a system admin connects
      providers from Profile, with fresh proof of the account);
    - ``blocked_by_owner``: ``provider_auto_link_blocked_at`` is set (the
      owner removed a provider, a reset removed one, or the owner declined
      a confirmation).

    Whether a provider of the same kind is linked already needs a query and
    is checked by the callers.
    """
    if not (
        user.email == _normalize_address(address)
        and user.auth_method == "password"
        and user.login_id is None
        and user.email_verified_at is not None
    ):
        return "not_an_email_signup_account"
    if user.totp_enabled:
        return "totp_enabled"
    if user.role != Role.USER.value:
        return "role"
    if user.provider_auto_link_blocked_at is not None:
        return "blocked_by_owner"
    return None


def account_stamp(user: _UserModel) -> str:
    """A digest of the account's security state, for a pending link (#2786).

    Covers the row id and user id, the address, the password hash, the MFA
    generation, two-step verification, the role and the block marker: a
    password change or reset, an MFA change, a role change, an address change
    or a removed provider after the confirmation mail was sent makes the
    pending link fail. An ordinary sign-in changes none of them.
    """
    blocked = user.provider_auto_link_blocked_at
    state = [
        user.id,
        user.user_id,
        user.email,
        user.password_hash,
        user.mfa_generation,
        bool(user.totp_enabled),
        user.role,
        blocked.isoformat() if blocked is not None else None,
    ]
    return hashlib.sha256(json.dumps(state).encode()).hexdigest()


def stage_address_link(
    db: AsyncSession,
    *,
    owner_id: str,
    provider: str,
    oauth_sub: str,
    action: str,
    actor_email: str,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """Stage the link row and its audit row of an address link; never commits.

    The one writer for both ways a provider identity is connected to an
    email sign-up account by its address: automatically (#1734,
    ``oauth_provider_auto_linked``) and after an email confirmation (#2786,
    ``oauth_provider_link_confirmed``). ``linked_at`` is now, so the new link
    proves nothing for identity linking during the link window
    (``provider_link_established``).
    """
    from models.auth import UserOAuthProvider
    from services.account_linking_service import stage_provider_audit

    now = utcnow()
    db.add(
        UserOAuthProvider(
            user_id=owner_id,
            provider=provider,
            oauth_sub=oauth_sub,
            linked_at=now,
            last_used_at=now,
        )
    )
    stage_provider_audit(
        db,
        user_id=owner_id,
        actor_email=actor_email,
        action=action,
        provider=provider,
        ip_address=ip_address,
        user_agent=user_agent,
    )


async def stage_email_account(
    db: AsyncSession,
    *,
    user_id: str,
    email: str,
    name: str | None,
    password_hash: str,
    locale: str | None,
    timezone: str | None,
) -> Role:
    """Add the ``users`` row of an email sign-up; never commits (#1734).

    The role comes from :func:`new_account_role`, the same rule as an OAuth
    sign-up. The address counts as verified (the person followed the link
    mailed to it). No provider and no login id: a later Google sign-in may
    link to it (``ensure_user``), the admin CLIs never take it for theirs.
    """
    from models.auth import User

    role = await new_account_role(db)
    prefs: dict[str, Any] = {}
    if is_valid_locale(locale):
        prefs["locale"] = locale
    if is_valid_timezone(timezone):
        prefs["timezone"] = timezone
    now = utcnow()
    db.add(
        User(
            email=_normalize_address(email),
            user_id=user_id,
            name=name,
            role=role.value,
            is_initial_admin=is_initial_admin_role(role),
            auth_method="password",
            password_hash=password_hash,
            login_id=None,
            auth_provider=None,
            email_verified_at=now,
            last_login_at=now,
            **prefs,
        )
    )
    return role


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
        email_authoritative: bool = False,
        ip_address: str | None = None,
        user_agent: str | None = None,
        locale: str | None = None,
        timezone: str | None = None,
        announcement_consent: SignupAnnouncementConsent | None = None,
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
            email_authoritative: Whether the IdP is authoritative for the
                mailbox (``provider_authoritative_for_address``, #1734). Drives
                ONLY the decision to link this identity to an account created
                by email sign-up for the same address; never the email sync.
            ip_address: Caller IP, captured for the audit log row when an email
                change is recorded.
            user_agent: Caller User-Agent, captured for the audit log row.
            locale: UI language the sign-in page was showing (#2700). Only a
                NEW row takes it; an existing account (including the row a
                concurrent first sign-in created) keeps its own. ``None`` or
                an unsupported value leaves the column default ``en``.
            timezone: IANA time zone the browser reported (#2700). Same rule
                as ``locale``; the column default is ``UTC``.
            announcement_consent: The announcement-email opt-in the sign-up
                page carried (#2761), already validated. Recorded (source
                ``signup``) only when this call creates the account, in the
                same commit as the ``users`` row; an existing account keeps its
                own answer. ``None`` records nothing ("never answered").

        Returns:
            The user's assigned role.

        Raises:
            ConflictError: When an email UPDATE would violate the
                ``users.email`` UNIQUE constraint (different account already
                holds the new address). The caller's transaction is rolled
                back; the row's prior state is preserved.
            UnlinkedProviderSignInError: When a google / github identity has
                no ``user_oauth_providers`` row but a ``users`` row carries
                its sub as ``user_id`` — the account removed this sign-in
                method. No role is returned and nothing is written.

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
                email_authoritative=email_authoritative,
                ip_address=ip_address,
                user_agent=user_agent,
                locale=locale,
                timezone=timezone,
                announcement_consent=announcement_consent,
            )

        # In-memory backend (testing only) — keyed by email for backward
        # compatibility with existing test suite. Postgres path uses user_id.
        if self._roles is None:
            self._roles = {}

        if email in self._roles:
            return self._roles[email]

        role = Role.ADMIN if len(self._roles) == 0 and session_admin_enabled() else Role.USER
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
        locale: str | None = None,
        timezone: str | None = None,
        email_authoritative: bool = False,
        announcement_consent: SignupAnnouncementConsent | None = None,
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

            # Resolve the owning user by (provider, oauth_sub) — the sole legacy-
            # free read path as of #938. The pre-#517 ``users.user_id == oauth_sub``
            # fallback and its self-heal insert were removed once the e37_517
            # backfill saturated: a prod probe confirmed 0 un-migrated
            # google/github users, so every such identity has a
            # ``user_oauth_providers`` row and this lookup is authoritative.
            # (``users.auth_provider`` is retained as the denormalized "primary"
            # pointer that account-linking writes. It no longer resolves the
            # owner, but #1811 reads it again to decide whether this sign-in
            # may sync email/name — see ``_sync_existing_user``.) A known
            # provider with no row falls through to the new-user path below,
            # whose IntegrityError(user_id) retry creates no duplicate and
            # signs in only when the row has appeared meanwhile (a concurrent
            # first sign-in); an identity whose row was removed is refused.
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

            if user is not None and link is not None:
                _adopt_primary_provider(user, auth_provider, link)
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
                    sync_profile=user.auth_provider == auth_provider,
                )

            # New user path. First user = ADMIN in session mode, else USER
            # (#2767: new_account_role is the one place for that rule).
            role = await new_account_role(db)

            # #1734: one account per address. Lock the address (every account
            # creation takes this lock), then look at the accounts holding it
            # in any case: an account created by email sign-up may take this
            # identity as a linked provider; any other holder ends in
            # ConflictError, never in a second account.
            await lock_address(db, email)
            linked_role = await self._link_or_refuse_address_holder(
                db=db,
                email=email,
                user_id=user_id,
                name=name,
                auth_provider=auth_provider,
                email_verified=email_verified,
                email_authoritative=email_authoritative,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            if linked_role is not None:
                return linked_role

            # #2700: seed the new row with the sign-in page's language and
            # time zone. Passed only when valid, so otherwise the column
            # defaults (``en`` / ``UTC``) apply. Never applied to an existing
            # row: neither _sync_existing_user nor the race retry below.
            prefs: dict[str, str] = {}
            if is_valid_locale(locale):
                prefs["locale"] = locale
            if is_valid_timezone(timezone):
                prefs["timezone"] = timezone

            new_user = User(
                email=email,
                user_id=user_id,
                name=name,
                role=role.value,
                # Issue #166: first admin protected from deletion/demotion.
                is_initial_admin=is_initial_admin_role(role),
                last_login_at=utcnow(),
                auth_provider=auth_provider,
                # #1752: only an address the IdP attested as verified counts
                # as verified (security notices go only to verified addresses).
                email_verified_at=utcnow() if email_verified else None,
                **prefs,
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
                # #2761: the sign-up opt-in, in the same transaction as the user
                # (and so rolled back with it on IntegrityError). Neither
                # _sync_existing_user nor the race retry below ever adds it, so
                # an existing account keeps its answer. The row has a foreign
                # key to users.user_id but no relationship(), so the unit of
                # work does not order the two inserts: flush the user first,
                # inside this try so a collision takes the handling below.
                if announcement_consent is not None:
                    from services.announcement_consent_service import new_consent_row

                    await db.flush()
                    db.add(
                        new_consent_row(
                            user_id=user_id,
                            value=announcement_consent.value,
                            source="signup",
                            wording_version=announcement_consent.wording_version,
                        )
                    )
                await db.commit()
                return role
            except IntegrityError as exc:
                # Three collision shapes share this except:
                #  (a) user_id race: another request just inserted the same
                #      oauth_sub — user and link row in one commit. Re-lookup
                #      by user_id and route the existing row through
                #      _sync_existing_user so concurrent first-logins still
                #      get last_login_at updated and any email/name drift
                #      synced (Copilot review #516).
                #  (b) email collision: oauth_sub is novel but email belongs
                #      to a different account (different provider, same
                #      address). The user_id re-lookup misses; raise 409.
                #  (c) an identity with no link row whose sub is still the
                #      ``user_id`` of the account it created: its owner
                #      removed this sign-in method (AccountLinkingService.
                #      unlink keeps the id). Only the link row tells (a) from
                #      (c), so a known provider is re-checked for its own
                #      row after the rollback and refused without one.
                await db.rollback()
                retry = await db.execute(select(User).filter_by(user_id=user_id))
                existing = retry.scalar_one_or_none()
                if existing is not None:
                    if known_provider:
                        # ``existing`` was found by ``user_id`` alone. Sign in
                        # through it only when this identity's link row exists
                        # and points at it (race (a)); a row that merely
                        # shares the id is not this identity's account.
                        own_link = (
                            await db.execute(
                                select(UserOAuthProvider).filter_by(
                                    provider=auth_provider, oauth_sub=user_id
                                )
                            )
                        ).scalar_one_or_none()
                        if own_link is None or own_link.user_id != existing.user_id:
                            logger.warning(
                                "oauth_unlinked_identity_sign_in_refused",
                                auth_provider=auth_provider,
                                user_id=user_id,
                                link_row_present=own_link is not None,
                            )
                            raise UnlinkedProviderSignInError() from exc
                        own_link.last_used_at = utcnow()
                        _adopt_primary_provider(existing, auth_provider, own_link)
                    return await self._sync_existing_user(
                        db=db,
                        user=existing,
                        new_email=email,
                        new_name=name,
                        auth_provider=auth_provider,
                        email_verified=email_verified,
                        ip_address=ip_address,
                        user_agent=user_agent,
                        sync_profile=existing.auth_provider == auth_provider,
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

    async def _link_or_refuse_address_holder(
        self,
        *,
        db: AsyncSession,
        email: str,
        user_id: str,
        name: str | None,
        auth_provider: str | None,
        email_verified: bool,
        email_authoritative: bool,
        ip_address: str | None,
        user_agent: str | None,
    ) -> Role | None:
        """Decide what a new identity does with an address that has an account (#1734).

        Runs on the new-user path under :func:`lock_address`. Returns ``None``
        to let the insert proceed (no account holds the address, or the
        identity's own removed account exists: the insert's IntegrityError
        arms answer that as before). Returns the role after linking this
        identity to the account email sign-up created for the address.
        Raises ``ConflictError`` (``email_in_use``) for every other holder:
        an account of another kind, one whose address differs only in case,
        several holders, or an email sign-up account that may not be linked
        automatically. Never a second account.

        The account is linked only when all of these hold, read under its row
        lock (an unlink or a password reset holds the same lock):

        - the provider is authoritative for the mailbox (``email_authoritative``,
          which needs a verified address) and the address is ASCII;
        - the account was created by email sign-up (``email_signup_account_clause``);
        - no two-step verification (an OAuth sign-in never asks for it);
        - an ordinary ``user`` role (never into a system admin);
        - the owner never removed a provider and no reset removed one
          (``provider_auto_link_blocked_at``);
        - no provider of this kind is linked yet.

        The new link proves nothing for identity linking during the link
        window (``provider_link_established``), never overwrites the account's
        name or address (``sync_profile=False``), and the owner is told.

        #2786: when everything but the provider's authority holds (a GitHub
        sign-in, or a Google one without authority over the mailbox) and the
        address is verified and ASCII, raises ``ProviderLinkConfirmable`` (a
        ``ConflictError``) instead: the callback may then mail the account a
        confirmation link. Authority is checked last for that reason, so the
        exclusions above apply to the confirmation as they do to the
        automatic link. ``ENABLE_PROVIDER_LINK_CONFIRMATION=false`` keeps the
        plain refusal.
        """
        from sqlalchemy import or_, select
        from sqlalchemy.exc import IntegrityError

        from models.auth import User, UserOAuthProvider

        normalized = _normalize_address(email)
        holders = list(
            (
                await db.execute(
                    select(User)
                    .where(or_(User.user_id == user_id, func.lower(User.email) == normalized))
                    .order_by(User.id)
                    .limit(3)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        if not holders or any(h.user_id == user_id for h in holders):
            return None

        def _refuse(reason: str) -> ConflictError:
            logger.warning(
                "oauth_email_collision_attempt",
                auth_provider=auth_provider,
                new_email_hmac=hmac_sha256_hex(email, get_settings().audit_hmac_key),
                user_id=user_id,
                phase="create",
                reason=reason,
            )
            return ConflictError("Email address is already in use by another account")

        if len(holders) != 1:
            raise _refuse("several_accounts")
        candidate = holders[0]
        if auth_provider not in ("google", "github"):
            raise _refuse("unknown_provider")
        # #2786: authority over the mailbox is decided last. Every other
        # refusal runs first, so a sign-in without authority is offered a
        # confirmation by email only for an account that could take the link.
        authoritative = email_authoritative and email_verified and email.isascii()
        if not (email_verified and email.isascii()):
            raise _refuse("provider_not_authoritative")
        refusal = email_account_link_refusal(candidate, address=email)
        if refusal is not None:
            raise _refuse(refusal)
        already = (
            await db.execute(
                select(UserOAuthProvider.id)
                .where(
                    UserOAuthProvider.user_id == candidate.user_id,
                    UserOAuthProvider.provider == auth_provider,
                )
                .limit(1)
            )
        ).first()
        if already is not None:
            raise _refuse("provider_already_linked")
        if not authoritative:
            if not get_settings().enable_provider_link_confirmation:
                raise _refuse("provider_not_authoritative")
            # Same message as every refusal: an unhandled one is email_in_use.
            logger.warning(
                "oauth_email_collision_attempt",
                auth_provider=auth_provider,
                new_email_hmac=hmac_sha256_hex(email, get_settings().audit_hmac_key),
                user_id=user_id,
                phase="create",
                reason="confirmation_offered",
            )
            raise ProviderLinkConfirmable(
                account_user_id=candidate.user_id, account_stamp=account_stamp(candidate)
            )

        owner_id = candidate.user_id
        stage_address_link(
            db,
            owner_id=owner_id,
            provider=auth_provider,
            oauth_sub=user_id,
            action="oauth_provider_auto_linked",
            actor_email=_OAUTH_CALLBACK_ACTOR,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        try:
            role = await self._sync_existing_user(
                db=db,
                user=candidate,
                new_email=email,
                new_name=name,
                auth_provider=auth_provider,
                email_verified=email_verified,
                ip_address=ip_address,
                user_agent=user_agent,
                sync_profile=False,
            )
        except IntegrityError:
            # The same identity's first sign-in ran in another tab and linked
            # it first (UNIQUE provider + sub). Sign in through that link when
            # it points at this account; anything else is a real failure.
            await db.rollback()
            own_link = (
                await db.execute(
                    select(UserOAuthProvider).filter_by(provider=auth_provider, oauth_sub=user_id)
                )
            ).scalar_one_or_none()
            if own_link is None or own_link.user_id != owner_id:
                raise
            owner = (
                await db.execute(select(User).filter_by(user_id=owner_id))
            ).scalar_one_or_none()
            if owner is None:
                raise
            own_link.last_used_at = utcnow()
            return await self._sync_existing_user(
                db=db,
                user=owner,
                new_email=email,
                new_name=name,
                auth_provider=auth_provider,
                email_verified=email_verified,
                ip_address=ip_address,
                user_agent=user_agent,
                sync_profile=False,
            )

        logger.info("oauth_provider_auto_linked", user_id=owner_id, provider=auth_provider)
        from services.security_notification_service import (
            PROVIDER_SIGN_IN_LABELS,
            SecurityEvent,
            spawn_security_notification,
        )

        spawn_security_notification(
            user_id=owner_id,
            event=SecurityEvent.SIGN_IN_METHOD_AUTO_LINKED,
            ip=ip_address,
            user_agent=user_agent,
            sign_in_method=PROVIDER_SIGN_IN_LABELS.get(auth_provider, auth_provider),
        )
        return role

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
        sync_profile: bool,
    ) -> Role:
        """Sync mutable attributes (email, name) on an existing user row.

        ``sync_profile`` is False when the identity signing in is not the
        account's primary one (#1811): a provider linked to the account
        (#517) whose name differs from ``users.auth_provider``. Its email and
        name then never overwrite the owner's, so no audit row or notice is
        written; ``last_login_at`` is still updated, and ``email_verified_at``
        is set only when it attests the account's own address.

        Email syncs only when ``email_verified`` is True AND the value
        differs. Name syncs whenever provided and different. UPDATE-collision
        on ``users.email`` UNIQUE raises ``ConflictError`` (rolled back).

        ``email_verified_at`` (#1752) is set when a verified email is synced,
        and when the IdP attests the unchanged current address as verified
        while it is still unset (verified on sign-in). An unverified
        assertion never sets it. After a synced email change commits, the
        previous address — when it was verified — is sent a security notice
        (``spawn_email_change_notification``).
        """
        from sqlalchemy.exc import IntegrityError

        from models.auth import AuditLog

        sync_email = sync_profile and email_verified and user.email != new_email
        sync_name = sync_profile and new_name is not None and user.name != new_name
        if not sync_profile:
            logger.debug(
                "oauth_profile_sync_skipped_linked_provider",
                auth_provider=auth_provider,
                user_id=user.user_id,
            )
        if email_verified and user.email == new_email and user.email_verified_at is None:
            user.email_verified_at = utcnow()
        if sync_email and await _address_held_by_another(db, new_email, user.user_id):
            # #1734: the unique index is case-sensitive, so the UPDATE would
            # pass for a case variant and leave two accounts with one
            # address (the other one's sign-in by address then fails
            # closed). Keep this account's address; the sign-in goes on.
            sync_email = False
            logger.warning(
                "oauth_email_sync_skipped_address_in_use",
                auth_provider=auth_provider,
                user_id=user.user_id,
                new_email_hmac=hmac_sha256_hex(new_email, get_settings().audit_hmac_key),
            )

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
        existing_email_verified = user.email_verified_at is not None
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
                user.email_verified_at = utcnow()
            if sync_name:
                user.name = new_name
            user.last_login_at = utcnow()
            await db.commit()
            if sync_email and existing_email_verified:
                # #1752: notices follow the account's address, so the previous
                # (verified) address is told that it no longer gets them.
                from services.security_notification_service import (
                    spawn_email_change_notification,
                )

                spawn_email_change_notification(
                    user_id=user_user_id,
                    old_email=existing_email,
                    ip=ip_address,
                    user_agent=user_agent,
                    auth_provider=auth_provider,
                )
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
