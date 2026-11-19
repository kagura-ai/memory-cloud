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

#2930: a share key never outlives its creator's right to share the context.
The share-key dependency honours a key only while ``creator_can_share`` holds
for its creator and bound context (checked per request, nothing cached); a
member's removal revokes that member's share keys for the workspace's contexts
(``revoke_creator_keys``), an account's deletion or erasure deletes them; and a
caller who may share a context revokes and lists every key bound to it.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from config.settings import get_settings
from models.auth import Context, ShareKey
from services.operation_history_writer import record_operation
from utils.datetime import utcnow
from utils.hashing import sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)


async def creator_can_share(db: AsyncSession, user_id: str, context_id: UUID) -> bool:
    """Whether ``user_id`` may share ``context_id`` (#2930).

    The rule the mint route applies: OWNER-level access to the context under
    ``PermissionService``'s role decision. On a shared context the workspace
    owner or an admin, or a member holding the context's owner role whose
    access list reaches it; on a private context only its creator (or an
    account linked to the creator); always with a plan seat. ``False`` for
    every refusal of that decision (not found, role too low, no seat). Any
    other failure propagates: the caller refuses on it, never allows.
    """
    from auth.workspace_roles import ContextRole
    from services.permission_service import PermissionService
    from utils.exceptions import AuthorizationError, FeatureNotAvailableError, NotFoundException

    try:
        await PermissionService(db).check_context_rbac(user_id, context_id, ContextRole.OWNER)
    except (NotFoundException, AuthorizationError, FeatureNotAvailableError):
        return False
    return True


def _record_revocation(db: AsyncSession, record: ShareKey, *, actor_user_id: str | None) -> None:
    """The ``credential.revoked`` operation record of one share key."""
    record_operation(
        db,
        operation="credential.revoked",
        actor_user_id=actor_user_id,
        context_id=record.context_id,
        target_id=record.public_id,
        subject_user_id=record.user_id,
        event_key=f"share-key-revoked:{record.public_id}",
    )


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
        record_operation(
            self.db,
            operation="credential.issued",
            actor_user_id=user_id,
            context_id=context_id,
            target_id=new_key.public_id,
            subject_user_id=user_id,
            event_key=f"share-key-issued:{new_key.public_id}",
        )

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

    async def list_context_keys(self, context_id: UUID) -> list[ShareKey]:
        """Every share key bound to ``context_id``, all creators, newest first (#2930).

        The caller has checked that the requester may share the context.
        """
        result = await self.db.execute(
            select(ShareKey)
            .where(ShareKey.context_id == context_id)
            .order_by(ShareKey.created_at.desc(), ShareKey.id.desc())
        )
        return list(result.scalars().all())

    async def revoke_key(self, public_id: str, user_id: str) -> bool:
        """Revoke a share key by its public id (soft delete, preserves audit trail).

        #2930: allowed for the key's creator, or for a caller who may share
        the key's bound context (``creator_can_share``: on a shared context the
        workspace owner or an admin, or a member holding the context's owner
        role whose access list reaches it; on a private context only its
        creator or an account linked to it). An unknown id, an already revoked key
        and a key the caller may not revoke all return ``False`` (the route
        maps it to the uniform 404). The operation record names the caller as
        actor and the creator as subject.
        """
        result = await self.db.execute(
            select(ShareKey).where(
                and_(
                    ShareKey.public_id == public_id,
                    ShareKey.revoked_at.is_(None),
                )
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            return False
        if record.user_id != user_id and not await creator_can_share(
            self.db, user_id, record.context_id
        ):
            return False

        record.revoked_at = utcnow()
        _record_revocation(self.db, record, actor_user_id=user_id)
        await self.db.flush()
        logger.info("share_key_revoked", key_id=record.id, public_id=public_id, user_id=user_id)
        return True

    async def revoke_creator_keys(
        self, user_id: str, *, workspace_id: UUID, actor_user_id: str | None
    ) -> int:
        """Revoke ``user_id``'s live share keys bound to contexts of ``workspace_id``.

        #2930: the member-removal path, inside its transaction. One
        ``credential.revoked`` record per key (actor ``actor_user_id``, subject
        the creator). Idempotent: a second run matches no live key and records
        nothing. Locks the matching ``share_keys`` rows in id order
        (``FOR UPDATE``) before the UPDATE; the contexts are read, not locked.

        Returns:
            How many keys were revoked.
        """

        def live(k: Any) -> tuple[Any, ...]:
            return (
                k.user_id == user_id,
                k.revoked_at.is_(None),
                k.context_id.in_(select(Context.id).where(Context.workspace_id == workspace_id)),
            )

        locked = aliased(ShareKey)
        rows = (
            await self.db.execute(
                update(ShareKey)
                .where(
                    ShareKey.id.in_(
                        select(locked.id).where(*live(locked)).order_by(locked.id).with_for_update()
                    ),
                    *live(ShareKey),
                )
                .values(revoked_at=utcnow())
                .returning(ShareKey)
                .execution_options(synchronize_session=False)
            )
        ).scalars()
        revoked = list(rows)
        for record in revoked:
            _record_revocation(self.db, record, actor_user_id=actor_user_id)
        if revoked:
            await self.db.flush()
            logger.info(
                "share_keys_revoked_on_departure",
                user_id=user_id,
                workspace_id=str(workspace_id),
                count=len(revoked),
            )
        return len(revoked)

    async def delete_creator_keys(self, user_id: str) -> int:
        """Delete every share key ``user_id`` created (#2930).

        The account deletion path, next to its API keys' delete. Locks the
        rows in id order (``FOR UPDATE``) before the DELETE.

        Returns:
            How many keys were deleted.
        """
        from sqlalchemy import delete

        locked = aliased(ShareKey)
        result = await self.db.execute(
            delete(ShareKey)
            .where(
                ShareKey.id.in_(
                    select(locked.id)
                    .where(locked.user_id == user_id)
                    .order_by(locked.id)
                    .with_for_update()
                )
            )
            .returning(ShareKey.id)
        )
        return len(result.scalars().all())
