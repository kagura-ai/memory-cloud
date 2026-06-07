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
- Unlinking the legacy "primary" provider repoints ``User.auth_provider`` to a
  surviving linked provider, or ``None`` when none remain (edge case 7).
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
    ) -> None:
        """Bind ``(provider, oauth_sub)`` to ``user_id`` (3-arm, audited).

        Args:
            user_id: The user that should own the identity.
            provider: OAuth provider name (``google`` / ``github``).
            oauth_sub: Provider-issued subject identifier.
            email: Actor email, recorded in the audit row.
            ip_address: Client IP for the audit row, if available.
            user_agent: Client user agent for the audit row, if available.

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
            return

        # arm 3 (other): identity owned by someone else — audit the failure, reject.
        if existing is not None:
            self._audit(
                user_id, email, "oauth_provider_link_failed", provider, ip_address, user_agent
            )
            await self.db.commit()
            logger.warning("oauth_provider_link_conflict", user_id=user_id, provider=provider)
            raise ConflictError("This provider is already linked to a different account")

        # arm 1 (unbound): INSERT the link + audit success.
        self.db.add(
            UserOAuthProvider(
                user_id=user_id,
                provider=provider,
                oauth_sub=oauth_sub,
                last_used_at=utcnow(),
            )
        )
        self._audit(user_id, email, "oauth_provider_linked", provider, ip_address, user_agent)
        await self.db.commit()
        logger.info("oauth_provider_linked", user_id=user_id, provider=provider)

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
        rows = await self.list_providers(user_id)
        user = (await self.db.execute(select(User).where(User.user_id == user_id))).scalar_one()

        target = next((r for r in rows if r.provider == provider), None)
        if target is None:
            raise NotFoundException("OAuth provider", resource_id=provider)

        has_password = user.auth_method == "password" and user.password_hash is not None
        remaining_methods = (len(rows) - 1) + (1 if has_password else 0)
        if remaining_methods < 1:
            raise ConflictError("Cannot unlink the only remaining sign-in method")

        await self.db.delete(target)
        # Edge case 7: repoint the legacy "primary" pointer off the removed provider.
        if user.auth_provider == provider:
            user.auth_provider = next((r.provider for r in rows if r.provider != provider), None)
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
