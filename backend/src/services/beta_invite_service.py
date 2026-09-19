"""Closed-beta invite link service (Issue #1581).

A signed-in user mints a one-time ``/join/{token}`` URL; whoever opens it may
pass the admin-configured signup gate once, within :data:`BETA_INVITE_TTL`.
This module owns every statement against ``beta_invites``. The redemption
itself is driven by ``SignupGateService`` (it owns ``signup_allowlist`` writes
and knows when the gate would otherwise block); it calls
:meth:`BetaInviteService.find_redeemable` and
:meth:`BetaInviteService.mark_redeemed` here so the invite-side SQL stays in
one place.

Security contract, in one paragraph: the token is ``secrets.token_urlsafe(32)``
and only ``sha256_hex(token)`` is ever stored (the API-key pattern,
``auth/api_keys.py``). The plaintext leaves this module exactly once, inside the
URL returned by :meth:`BetaInviteService.create`. Nothing here logs or audits
the token, its hash, or the URL — the invite ``id`` is the only identifier that
may appear in a log line.
"""

from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Update, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.roles import Role
from config.settings import get_settings
from models.auth import AuditLog, User
from models.beta_invite import BetaInvite
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import (
    BetaInviteAlreadyRedeemedError,
    BetaInviteGoneError,
    BetaInviteQuotaExceededError,
    NotFoundException,
)
from utils.hashing import sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

# Fixed by design (#1581) — a code constant, not a setting: a link that lingers
# for months is a standing credential, and nothing about a deployment changes
# how long "come join the beta" stays a reasonable thing to act on.
BETA_INVITE_TTL = timedelta(days=7)

# 32 bytes -> 43 URL-safe characters, 256 bits. Enumerating the public preview
# endpoint is infeasible at that size; the per-IP limiter is belt-and-braces.
_TOKEN_BYTES = 32

# The shape anything claiming to be an invite token must have before it is
# hashed or looked up: the ``token_urlsafe`` alphabet, bounded length. Single
# source of truth for the OAuth login's ``invite=`` parameter and the public
# preview route. Deliberately looser than "exactly 43" so a future change to
# ``_TOKEN_BYTES`` does not strand links already in people's inboxes.
BETA_INVITE_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{20,128}$"


def build_beta_invite_url(token: str) -> str:
    """Build the absolute invite landing URL from ``FRONTEND_URL``.

    ``/join`` is deliberately distinct from the workspace-member invitation's
    ``/invite/{token}`` (``services/invitation_service.build_invitation_url``):
    the two are different layers and must not be conflated (#358).

    Args:
        token: The plaintext invite token (single-use; treat the returned URL
            as a credential — do not log it).

    Returns:
        ``f"{FRONTEND_URL}/join/{token}"``.
    """
    base_url = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base_url}/join/{token}"


def build_redeem_update(*, token_hash: str, allowlist_entry_id: uuid.UUID, now: datetime) -> Update:
    """Build the single-use claim ``UPDATE`` — THE double-redeem guard.

    Extracted so the emitted predicate is assertable without a database
    (``tests/services/test_beta_invite_service.py::TestRedeemUpdateSQL``). Two
    concurrent redemptions both reach this statement; the row lock serializes
    them and, under READ COMMITTED, the loser re-evaluates ``redeemed_at IS
    NULL`` against the winner's committed row and matches nothing.

    Args:
        token_hash: ``sha256_hex`` of the presented token.
        allowlist_entry_id: The ``signup_allowlist`` row written for the invitee.
        now: Naive UTC instant; both the redemption stamp and the expiry bound.

    Returns:
        The ``UPDATE`` statement. The caller must require ``rowcount == 1``.
    """
    return (
        update(BetaInvite)
        .where(
            BetaInvite.token_hash == token_hash,
            BetaInvite.redeemed_at.is_(None),
            BetaInvite.revoked_at.is_(None),
            BetaInvite.expires_at > now,
        )
        .values(redeemed_at=now, redeemed_allowlist_entry_id=allowlist_entry_id)
    )


@dataclass(frozen=True)
class BetaInviteSummary:
    """What a user sees about their own invites. ``None`` quota = unlimited."""

    quota: int | None
    used: int
    remaining: int | None
    invites: list[BetaInvite]


@dataclass(frozen=True)
class MintedBetaInvite:
    """A freshly minted invite plus the only copy of its plaintext URL."""

    invite: BetaInvite
    url: str


class BetaInviteService:
    """Mint, list, revoke and preview invite links; claim one at redemption."""

    def __init__(self, db: AsyncSession):
        """Initialize.

        Args:
            db: Async database session.
        """
        self.db = db
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # Inviter-facing
    # ------------------------------------------------------------------

    async def get_summary(self, user_id: str) -> BetaInviteSummary:
        """Return the caller's quota standing and their invites, newest first.

        Args:
            user_id: The caller's OAuth ``sub``.

        Returns:
            The summary. ``quota`` / ``remaining`` are ``None`` for a system admin.

        Raises:
            NotFoundException: The caller's ``users`` row is missing.
        """
        role = await self._inviter_role(user_id, lock=False)
        now = utcnow()
        used = await self._count_used(user_id, now)
        invites = list(
            await self.db.scalars(
                select(BetaInvite)
                .where(BetaInvite.inviter_user_id == user_id)
                .order_by(BetaInvite.created_at.desc())
            )
        )
        quota = self._quota_for(role)
        return BetaInviteSummary(
            quota=quota,
            used=used,
            remaining=None if quota is None else max(0, quota - used),
            invites=invites,
        )

    async def create(
        self,
        *,
        user_id: str,
        user_email: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> MintedBetaInvite:
        """Mint an invite link for ``user_id``, enforcing the per-user quota.

        The quota check and the insert share one transaction that first takes a
        row lock on the inviter, so parallel creates cannot both observe "one
        slot left". The role is read from that locked row rather than trusted
        from the session, so a demoted admin with a live session is capped.

        Args:
            user_id: The inviter's OAuth ``sub``.
            user_email: The inviter's e-mail, for the audit row's actor column.
            ip_address: Caller IP for the audit row.
            user_agent: Caller User-Agent for the audit row.

        Returns:
            The invite and its plaintext URL — the only time the URL exists.

        Raises:
            NotFoundException: The inviter's ``users`` row is missing.
            BetaInviteQuotaExceededError: The inviter is at the cap.
        """
        role = await self._lock_inviter_role(user_id)
        now = utcnow()
        quota = self._quota_for(role)
        if quota is not None:
            used = await self._count_used(user_id, now)
            if used >= quota:
                logger.info("beta_invite_quota_exceeded", inviter_user_id=user_id, used=used)
                raise BetaInviteQuotaExceededError(quota=quota)

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        invite = BetaInvite(
            # Client-side id so the audit row can name the invite before flush.
            id=uuid.uuid4(),
            token_hash=sha256_hex(token),
            inviter_user_id=user_id,
            created_at=now,
            expires_at=now + BETA_INVITE_TTL,
        )
        self.db.add(invite)
        self.db.add(
            self._audit(
                action="beta_invite.created",
                invite_id=invite.id,
                user_email=user_email,
                user_id=user_id,
                user_metadata={"expires_at": to_utc_iso(invite.expires_at)},
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )
        await self.db.commit()

        logger.info("beta_invite_created", invite_id=str(invite.id), inviter_user_id=user_id)
        return MintedBetaInvite(invite=invite, url=build_beta_invite_url(token))

    async def revoke(
        self,
        *,
        user_id: str,
        invite_id: uuid.UUID,
        user_email: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Revoke one of the caller's own unused invites. Idempotent.

        The revoke is a guarded ``UPDATE`` (``redeemed_at IS NULL``), the mirror
        image of the redeem claim (``revoked_at IS NULL``): whichever of a
        concurrent revoke / redeem commits first wins and the other matches no
        row, so an invite can never end up both redeemed and revoked.

        Args:
            user_id: The caller's OAuth ``sub``.
            invite_id: The invite to revoke.
            user_email: The caller's e-mail, for the audit row's actor column.
            ip_address: Caller IP for the audit row.
            user_agent: Caller User-Agent for the audit row.

        Raises:
            NotFoundException: No such invite, or it belongs to someone else —
                the same answer, so ids cannot be probed.
            BetaInviteAlreadyRedeemedError: The invite was already used.
        """
        result = await self.db.execute(
            update(BetaInvite)
            .where(
                BetaInvite.id == invite_id,
                BetaInvite.inviter_user_id == user_id,
                BetaInvite.redeemed_at.is_(None),
                BetaInvite.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        # ``AsyncSession.execute`` is typed as returning ``Result``; a DML
        # statement always yields a ``CursorResult`` at runtime.
        if cast(CursorResult[Any], result).rowcount == 1:
            self.db.add(
                self._audit(
                    action="beta_invite.revoked",
                    invite_id=invite_id,
                    user_email=user_email,
                    user_id=user_id,
                    user_metadata=None,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )
            await self.db.commit()
            logger.info("beta_invite_revoked", invite_id=str(invite_id), inviter_user_id=user_id)
            return

        # Nothing updated — work out why, for the caller's status code.
        invite = await self.db.scalar(
            select(BetaInvite).where(
                BetaInvite.id == invite_id, BetaInvite.inviter_user_id == user_id
            )
        )
        if invite is None:
            raise NotFoundException("Beta invite")
        if invite.redeemed_at is not None:
            raise BetaInviteAlreadyRedeemedError()
        # Already revoked: a retry or a double-click. Nothing to do, no new audit row.

    # ------------------------------------------------------------------
    # Public preview
    # ------------------------------------------------------------------

    async def preview(self, token: str) -> BetaInvite:
        """Resolve a plaintext token for the public landing page.

        Read-only: previewing never consumes or touches the invite.

        Args:
            token: The plaintext token from the URL.

        Returns:
            The invite, when it is still usable.

        Raises:
            NotFoundException: Unknown or revoked (indistinguishable on purpose —
                a revoked link should look like it never existed).
            BetaInviteGoneError: Expired or already redeemed.
        """
        invite = await self.db.scalar(
            select(BetaInvite).where(BetaInvite.token_hash == sha256_hex(token))
        )
        if invite is None:
            raise NotFoundException("Beta invite")
        status = invite.status
        if status == "revoked":
            raise NotFoundException("Beta invite")
        if status != "active":
            raise BetaInviteGoneError()
        return invite

    # ------------------------------------------------------------------
    # Redemption (driven by SignupGateService)
    # ------------------------------------------------------------------

    async def find_redeemable(self, token_hash: str, now: datetime) -> BetaInvite | None:
        """Return the invite for ``token_hash`` if it could be redeemed right now.

        A cheap pre-check so a dead token writes nothing. It is NOT the
        double-use guard — :meth:`mark_redeemed` is.

        Args:
            token_hash: ``sha256_hex`` of the presented token.
            now: Naive UTC instant to evaluate expiry against.

        Returns:
            The invite, or ``None`` when unknown / redeemed / revoked / expired.
        """
        return await self.db.scalar(
            select(BetaInvite).where(
                BetaInvite.token_hash == token_hash,
                BetaInvite.redeemed_at.is_(None),
                BetaInvite.revoked_at.is_(None),
                BetaInvite.expires_at > now,
            )
        )

    async def mark_redeemed(
        self, *, token_hash: str, allowlist_entry_id: uuid.UUID, now: datetime
    ) -> bool:
        """Atomically claim the invite. Does not commit — the caller owns the tx.

        Args:
            token_hash: ``sha256_hex`` of the presented token.
            allowlist_entry_id: The allowlist row written in the same transaction.
            now: Naive UTC instant (redemption stamp + expiry bound).

        Returns:
            True iff exactly one row was claimed. False means the caller lost a
            race (or the invite died since the pre-check) and MUST roll back.
        """
        result = await self.db.execute(
            build_redeem_update(
                token_hash=token_hash, allowlist_entry_id=allowlist_entry_id, now=now
            )
        )
        return cast(CursorResult[Any], result).rowcount == 1

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _quota_for(self, role: str) -> int | None:
        """``None`` (unlimited) for a system admin, else the configured cap."""
        if role == Role.ADMIN:
            return None
        return self.settings.beta_invite_quota_per_user

    async def _inviter_role(self, user_id: str, *, lock: bool) -> str:
        stmt = select(User.role).where(User.user_id == user_id)
        if lock:
            stmt = stmt.with_for_update()
        role = await self.db.scalar(stmt)
        if role is None:
            # A live session whose ``users`` row is gone (erased account).
            raise NotFoundException("User", user_id)
        return role

    async def _lock_inviter_role(self, user_id: str) -> str:
        """Row-lock the inviter for the rest of the transaction; return their role."""
        return await self._inviter_role(user_id, lock=True)

    async def _count_used(self, user_id: str, now: datetime) -> int:
        """Count the invites occupying a quota slot: active + redeemed.

        Expired-unused and revoked invites free their slot.
        """
        return (
            await self.db.scalar(
                select(func.count(BetaInvite.id)).where(
                    BetaInvite.inviter_user_id == user_id,
                    BetaInvite.revoked_at.is_(None),
                    or_(BetaInvite.redeemed_at.is_not(None), BetaInvite.expires_at > now),
                )
            )
            or 0
        )

    @staticmethod
    def _audit(
        *,
        action: str,
        invite_id: uuid.UUID,
        user_email: str,
        user_id: str,
        user_metadata: dict[str, Any] | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> AuditLog:
        """Build an ``audit_logs`` row that names the invite by ``id`` only."""
        return AuditLog(
            user_email=user_email,
            user_id=user_id,
            action=action,
            resource=f"beta_invite:{invite_id}",
            user_metadata=user_metadata,
            ip_address=ip_address,
            user_agent=user_agent,
        )
