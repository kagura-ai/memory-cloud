"""Multi-provider OAuth account linking service (#517 Task 4).

Lets a single user own several OAuth identities (e.g. google + github) via the
``user_oauth_providers`` table, with a hard invariant that an OAuth identity
``(provider, oauth_sub)`` belongs to at most one user. Every state change is
recorded in ``audit_logs`` — including *failed* link attempts, so a hijack
attempt against an already-bound identity leaves a trail (edge case 6).

Security edge cases enforced here:

- An identity already bound to a different user can never be re-pointed
  (3-arm ``link``: unbound INSERT / mine idempotent touch / other -> conflict).
- Unlink never strips a user of their last sign-in method (password counts).
- Unlinking the legacy "primary" provider repoints ``User.auth_provider``
  (edge case 7): for an OAuth account to the surviving row of the identity
  the account was created with (sub == ``user_id``), or ``None`` when that
  identity is gone; for a password account to any surviving provider, or
  ``None``. Linking never sets the pointer, and a provider attached here
  never becomes primary, by unlink or by sign-in: an OAuth account left with
  ``None`` gets the pointer back only on a sign-in through the identity it
  was created with, once that link is established (``RoleManager``, #1875).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import AuditLog, User, UserOAuthProvider
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException
from utils.hashing import hmac_sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)


class AccountLinkingService:
    """Link, unlink, and list a user's OAuth provider identities."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_providers(self, user_id: str) -> list[UserOAuthProvider]:
        """Return all ``UserOAuthProvider`` rows owned by ``user_id``."""
        result = await self.db.execute(
            select(UserOAuthProvider).where(UserOAuthProvider.user_id == user_id)
        )
        return list(result.scalars().all())

    async def link(
        self,
        *,
        user_id: str,
        provider: str,
        oauth_sub: str,
        email: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> bool:
        """Bind ``(provider, oauth_sub)`` to ``user_id`` (3-arm, audited).

        Args:
            user_id: The user that should own the identity.
            provider: OAuth provider name (``google`` / ``github``).
            oauth_sub: Provider-issued subject identifier.
            email: Actor email, recorded in the audit row.
            ip_address: Client IP for the audit row, if available.
            user_agent: Client user agent for the audit row, if available.

        Returns:
            True when a new link was written (the caller notifies the owner,
            #1752); False when the identity was already linked to this user.

        Raises:
            ConflictError: The identity is already bound to a different user.
                A ``oauth_provider_link_failed`` audit row is written first.
        """
        existing = (
            await self.db.execute(
                select(UserOAuthProvider).where(
                    UserOAuthProvider.provider == provider,
                    UserOAuthProvider.oauth_sub == oauth_sub,
                )
            )
        ).scalar_one_or_none()

        # arm 2 (mine): idempotent — refresh last_used_at, no duplicate, no error.
        if existing is not None and existing.user_id == user_id:
            existing.last_used_at = utcnow()
            await self.db.commit()
            return False

        # arm 3 (other): identity owned by someone else — audit the failure, reject.
        if existing is not None:
            self._audit(
                user_id, email, "oauth_provider_link_failed", provider, ip_address, user_agent
            )
            await self.db.commit()
            logger.warning("oauth_provider_link_conflict", user_id=user_id, provider=provider)
            raise ConflictError("This provider is already linked to a different account")

        # arm 1 (unbound): INSERT the link + audit success. ``linked_at`` is
        # written here rather than left to the column default: the OAuth
        # callbacks compare it with ``utcnow()`` to keep a provider attached
        # minutes ago from proving its account (#1875), so it must not depend
        # on the database session's time zone.
        now = utcnow()
        self.db.add(
            UserOAuthProvider(
                user_id=user_id,
                provider=provider,
                oauth_sub=oauth_sub,
                linked_at=now,
                last_used_at=now,
            )
        )
        self._audit(user_id, email, "oauth_provider_linked", provider, ip_address, user_agent)
        await self.db.commit()
        logger.info("oauth_provider_linked", user_id=user_id, provider=provider)
        return True

    async def unlink(
        self,
        *,
        user_id: str,
        provider: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Remove a linked provider, never leaving zero sign-in methods.

        Args:
            user_id: The owning user.
            provider: Provider to unlink.
            ip_address: Client IP for the audit row, if available.
            user_agent: Client user agent for the audit row, if available.

        Raises:
            NotFoundException: The provider is not linked to this account (404).
            ConflictError: Removing it would leave the user with no usable
                sign-in method (409).
        """
        # Lock the user row BEFORE reading the methods: a concurrent password
        # removal (PasswordAccountService.remove) or unlink locks it too, so
        # the later of the two sees the earlier one's commit and refuses
        # rather than both removing a method (#1678).
        user = (
            await self.db.execute(
                select(User)
                .where(User.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if user is None:
            # The user row is gone (deleted mid-flow). 404 gracefully rather
            # than letting NoResultFound bubble out as a global 503.
            raise NotFoundException("User", resource_id=user_id)
        rows = await self.list_providers(user_id)

        target = next((r for r in rows if r.provider == provider), None)
        if target is None:
            raise NotFoundException("OAuth provider", resource_id=provider)

        # #1678: any account with a password can sign in with it (by login id
        # or verified email), whatever its original ``auth_method``.
        has_password = user.password_hash is not None
        # As of #938 the ensure_user legacy ``users.user_id``-as-sub fallback is
        # gone (the e37_517 backfill saturated — a prod probe confirmed 0
        # un-migrated google/github users), so every usable OAuth sign-in method
        # is now represented by a ``user_oauth_providers`` row. Remaining methods
        # = the other linked providers + password; no separate legacy-provider
        # term is needed. (``users.auth_provider`` is still WRITTEN below as the
        # denormalized "primary" pointer. Sign-in no longer resolves the owner
        # through it, but ``RoleManager._sync_existing_user`` reads it to sync
        # email/name only from the primary provider — #1811.)
        remaining_methods = (len(rows) - 1) + (1 if has_password else 0)
        if remaining_methods < 1:
            raise ConflictError("Cannot unlink the only remaining sign-in method")

        await self.db.delete(target)
        # Edge case 7: repoint the legacy "primary" pointer off the removed
        # provider. For an OAuth account it follows the sign-in rule
        # (``RoleManager._adopt_primary_provider``): only the identity the
        # account was created with (sub == ``user_id``) may become the one
        # whose email and name are synced; a surviving provider attached
        # later — which needed only a live session — leaves the pointer NULL.
        # A password account keeps its pointer semantics (next survivor).
        if user.auth_provider == provider:
            survivors = [r for r in rows if r.provider != provider]
            if user.auth_method == "oauth":
                survivors = [r for r in survivors if r.oauth_sub == user.user_id]
            user.auth_provider = next((r.provider for r in survivors), None)
        self._audit(
            user_id, user.email, "oauth_provider_unlinked", provider, ip_address, user_agent
        )
        await self.db.commit()
        logger.info("oauth_provider_unlinked", user_id=user_id, provider=provider)

    def _audit(
        self,
        user_id: str,
        email: str,
        action: str,
        provider: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Stage an ``AuditLog`` row for an account-linking action.

        The provider name is HMAC-hashed into ``new_value_hash`` per the
        audit-log no-plaintext convention; the resource carries the readable
        ``oauth_provider:<provider>`` label for filtering.
        """
        key = get_settings().audit_hmac_key
        self.db.add(
            AuditLog(
                user_email=email,
                user_id=user_id,
                action=action,
                resource=f"oauth_provider:{provider}",
                new_value_hash=hmac_sha256_hex(provider, key),
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )
