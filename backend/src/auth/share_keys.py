"""Async manager for context-scoped, read-only, TTL-bounded share keys (#1027).

A share key is a shareable credential bound to **one** context, carrying
``memory:read`` scope only, and always time-limited. It is a composition of
three shipped primitives — context scope (#629/#626), read scope (#649), and
TTL/expiry (#889) — kept in its own ``share_keys`` table so the read-only and
confinement invariants are structural (see ``models.auth.ShareKey``).

This manager mirrors ``auth.api_keys.APIKeyManager`` deliberately: same SHA256
hashing, same constant-time verify gate (#964), and the same throttled
``last_used_at`` write (#947). It diverges only where the share-key semantics
require it — a mandatory ``context_id`` binding and a mandatory, clamped
``expires_at``.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import ShareKey
from utils.datetime import utcnow
from utils.hashing import sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

# Distinct prefix so a share key is visually identifiable and so it never
# collides with a personal ``kagura_`` API key in the ``api_keys`` table.
SHARE_KEY_PREFIX = "kagura_sk_"

# The only scope a share key ever carries (#1027 AC: memory-read only).
SHARE_KEY_SCOPE = "memory:read"

# TTL ceiling (#889 prior art: 30 days). A requested lifetime is clamped to
# this; omitting one defaults to the ceiling. Share keys never live forever.
MAX_TTL_DAYS = 30
DEFAULT_TTL_DAYS = 30


class VerifiedShareKey(NamedTuple):
    """Result of a successful share-key verification.

    Attributes:
        id: ``share_keys.id`` — surfaced for audit / rate-limit buckets.
        user_id: The minting owner's id (attribution only; the key never
            inherits the owner's workspace — confinement is via context_id).
        context_id: The single context this key is confined to. The
            share-recall surface forces recall to exactly this context.
    """

    id: int
    user_id: str
    context_id: UUID


class ShareKeyManager:
    """Async manager for ``share_keys`` rows."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _hash_key(share_key: str) -> str:
        return sha256_hex(share_key)

    @staticmethod
    def _generate_key() -> str:
        return f"{SHARE_KEY_PREFIX}{secrets.token_urlsafe(32)}"

    @staticmethod
    def _clamp_ttl_days(ttl_days: int | None) -> int:
        """Clamp a requested lifetime to ``[1, MAX_TTL_DAYS]``.

        ``None`` → ``DEFAULT_TTL_DAYS``. Values above the ceiling are pulled
        down to ``MAX_TTL_DAYS``; non-positive values floor to 1 day. This is
        the #889 ``min(requested, MAX)`` clamp, with a mandatory default so a
        share key is never minted without an expiry.
        """
        if ttl_days is None:
            return DEFAULT_TTL_DAYS
        return max(1, min(ttl_days, MAX_TTL_DAYS))

    async def create_key(
        self,
        name: str,
        user_id: str,
        context_id: UUID,
        ttl_days: int | None = None,
    ) -> tuple[str, ShareKey]:
        """Mint a share key bound to ``context_id``.

        The caller is responsible for verifying that ``user_id`` actually owns
        ``context_id`` (the route does this via
        ``PermissionService.check_context_access(..., OWNER)``); this manager
        only enforces share-key invariants — single-context binding, fixed
        read scope, and a clamped mandatory expiry.

        Args:
            name: Friendly name (unique per active key for the user).
            user_id: Minting owner.
            context_id: The single context to confine the key to.
            ttl_days: Requested lifetime in days; clamped to
                ``[1, MAX_TTL_DAYS]``. ``None`` → ``DEFAULT_TTL_DAYS``.

        Returns:
            ``(plaintext_key, share_key_row)`` — the plaintext is only ever
            available here; everything persisted is the SHA256 hash.

        Raises:
            ValueError: if an active key with ``name`` already exists for the
                user.
        """
        # Name uniqueness among the user's *currently active* share keys —
        # non-revoked AND non-expired. An already-expired key (rejected on use
        # anyway) must not block re-minting a replacement under the same name;
        # share keys are short-lived and re-issuing the same label after expiry
        # is a normal workflow. Uniqueness is enforced at the app layer only
        # (mirroring APIKey): the name is a non-security display label —
        # revoke is by id — so a rare concurrent same-name race is harmless.
        result = await self.db.execute(
            select(ShareKey).where(
                and_(
                    ShareKey.name == name,
                    ShareKey.user_id == user_id,
                    ShareKey.revoked_at.is_(None),
                    ShareKey.expires_at > utcnow(),
                )
            )
        )
        if result.scalar_one_or_none() is not None:
            raise ValueError(f"Share key with name '{name}' already exists for this user")

        share_key = self._generate_key()
        key_hash = self._hash_key(share_key)
        key_prefix = share_key[:16]
        expires_at = utcnow() + timedelta(days=self._clamp_ttl_days(ttl_days))

        new_key = ShareKey(
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=name,
            user_id=user_id,
            context_id=context_id,
            scope=SHARE_KEY_SCOPE,
            expires_at=expires_at,
        )
        self.db.add(new_key)
        await self.db.flush()

        logger.info(
            "share_key_created",
            name=name,
            user_id=user_id,
            context_id=str(context_id),
            expires_at=expires_at.isoformat(),
        )

        return share_key, new_key

    async def verify_key(self, share_key: str) -> VerifiedShareKey | None:
        """Verify a plaintext share key.

        Returns ``None`` (not an exception) for every rejection — not found,
        revoked, or expired — so the auth layer treats them uniformly as
        "invalid credential" without leaking which gate failed.

        Args:
            share_key: Plaintext key (``kagura_sk_...``).

        Returns:
            ``VerifiedShareKey`` if valid, else ``None``.
        """
        key_hash = self._hash_key(share_key)

        result = await self.db.execute(select(ShareKey).where(ShareKey.key_hash == key_hash))
        record = result.scalar_one_or_none()

        if not record:
            return None

        # #964: constant-time compare closes the B-tree query-timing oracle —
        # do not trust the surfaced row until the hash matches exactly.
        if not secrets.compare_digest(record.key_hash, key_hash):
            return None

        if record.revoked_at:
            return None

        # Mandatory expiry — expired keys are rejected on use (#1027 AC).
        if utcnow() > record.expires_at:
            return None

        # Throttled last_used_at write (#947) — at most one UPDATE per window.
        now = utcnow()
        throttle = timedelta(seconds=get_settings().api_key_last_used_throttle_seconds)
        if record.last_used_at is None or now - record.last_used_at >= throttle:
            record.last_used_at = now
            await self.db.flush()

        return VerifiedShareKey(
            id=record.id,
            user_id=record.user_id,
            context_id=record.context_id,
        )

    async def list_keys(self, user_id: str) -> list[ShareKey]:
        """List a user's share keys, newest first."""
        result = await self.db.execute(
            select(ShareKey).where(ShareKey.user_id == user_id).order_by(ShareKey.created_at.desc())
        )
        return list(result.scalars().all())

    async def revoke_key(self, key_id: int, user_id: str) -> bool:
        """Revoke a share key by id (soft delete, preserves audit trail).

        Ownership is enforced in the query — a key id that is not the user's
        active key returns ``False`` (the route maps that to a uniform 404).
        """
        result = await self.db.execute(
            select(ShareKey).where(
                and_(
                    ShareKey.id == key_id,
                    ShareKey.user_id == user_id,
                    ShareKey.revoked_at.is_(None),
                )
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            return False

        record.revoked_at = utcnow()
        await self.db.flush()
        logger.info("share_key_revoked", key_id=key_id, user_id=user_id)
        return True
