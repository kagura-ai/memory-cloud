"""Closed-beta invite link service (Issues #1581, #1595).

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
URL returned by :meth:`BetaInviteService.create` (or
:meth:`BetaInviteService.reissue`). Nothing here logs or audits the token, its
hash, or the URL — the invite ``id`` is the only identifier that may appear in a
log line.

Privacy contract (#1595): an invite's ``label`` is the inviter's free-text note
and may hold a name or an address; ``redeemed_email`` is the admitted account's
address. Both go to the inviter's own responses and nowhere else — never a log
line, an audit row or an exception message.
"""

from __future__ import annotations

import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, Update, and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.roles import Role
from config.settings import get_settings
from models.auth import AuditLog, User, UserOAuthProvider
from models.beta_invite import BetaInvite
from models.signup_gate import SignupAllowlistEntry
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import (
    BetaInviteAlreadyRedeemedError,
    BetaInviteAlreadyRevokedError,
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

# #1595: mirrors ``beta_invites.label`` (``VARCHAR(100)``). Python ``len`` and
# Postgres ``varchar(n)`` both count characters, so the two bounds agree.
BETA_INVITE_LABEL_MAX_LENGTH = 100

# C0 controls + DEL. A label is one line of text for a list row; a newline, a
# NUL or an escape sequence in it is never something a person typed on purpose.
_LABEL_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def normalize_beta_invite_label(raw: str | None) -> str | None:
    """Normalise an inviter-supplied label, or refuse it (#1595).

    Surrounding whitespace is trimmed first, so only what would be stored is
    validated (a pasted trailing newline is forgiven); an empty result means
    "no label".

    Args:
        raw: The label as sent by the client, or ``None``.

    Returns:
        The trimmed label, or ``None`` when absent / blank.

    Raises:
        ValueError: Longer than :data:`BETA_INVITE_LABEL_MAX_LENGTH` after the
            trim, or containing a control character. The message never contains
            the label — it ends up in the 422 body.
    """
    if raw is None:
        return None
    label = raw.strip()
    if not label:
        return None
    if len(label) > BETA_INVITE_LABEL_MAX_LENGTH:
        raise ValueError(f"label must be at most {BETA_INVITE_LABEL_MAX_LENGTH} characters")
    if _LABEL_CONTROL_CHARS.search(label):
        raise ValueError("label must not contain control characters")
    return label


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


def build_revoke_update(*, invite_id: uuid.UUID, user_id: str, now: datetime) -> Update:
    """Build the guarded revoke ``UPDATE`` shared by ``DELETE`` and reissue (#1595).

    The mirror image of :func:`build_redeem_update` (``redeemed_at IS NULL`` here,
    ``revoked_at IS NULL`` there): whichever of a concurrent revoke / redeem
    commits first wins and the other matches no row. No expiry bound — an
    expired link is still revocable and reissuable. ``inviter_user_id`` is part
    of the predicate, so someone else's invite is indistinguishable from an
    unknown id.

    Args:
        invite_id: The invite to revoke.
        user_id: The caller — must be the inviter.
        now: Naive UTC instant; the revocation stamp.

    Returns:
        The ``UPDATE … RETURNING label`` statement. The caller must require
        exactly one returned row.
    """
    return (
        update(BetaInvite)
        .where(
            BetaInvite.id == invite_id,
            BetaInvite.inviter_user_id == user_id,
            BetaInvite.redeemed_at.is_(None),
            BetaInvite.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .returning(BetaInvite.label)
    )


def build_slot_counts_select(*, user_id: str, now: datetime) -> Select[tuple[int, int]]:
    """Build the one-statement ``(active, redeemed)`` aggregate (#1595).

    THE definition of "occupies a quota slot": an unrevoked invite that is either
    still redeemable (``active``) or already redeemed. Expired-unused and revoked
    invites free their slot. :meth:`BetaInviteService._count_used` is the sum of
    these two, so the header breakdown and the quota gate cannot drift apart.
    The buckets follow :meth:`BetaInvite.status_at` exactly.

    Args:
        user_id: The inviter.
        now: Naive UTC instant to evaluate expiry against.

    Returns:
        A ``SELECT`` yielding one ``(active, redeemed)`` row.
    """
    return select(
        func.count(BetaInvite.id)
        .filter(BetaInvite.redeemed_at.is_(None), BetaInvite.expires_at > now)
        .label("active"),
        func.count(BetaInvite.id).filter(BetaInvite.redeemed_at.is_not(None)).label("redeemed"),
    ).where(BetaInvite.inviter_user_id == user_id, BetaInvite.revoked_at.is_(None))


@dataclass(frozen=True)
class BetaInviteSummary:
    """What a user sees about their own invites. ``None`` quota = unlimited.

    ``used == active + redeemed`` always (one aggregate, see
    :func:`build_slot_counts_select`). ``redeemed_emails`` maps the id of each
    ``redeemed`` invite whose admitted account still exists to that account's
    current e-mail; every other invite is absent from it.
    """

    quota: int | None
    used: int
    active: int
    redeemed: int
    remaining: int | None
    invites: list[BetaInvite]
    redeemed_emails: dict[uuid.UUID, str]


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
        active, redeemed = await self._count_slots(user_id, now)
        used = active + redeemed

        # One statement for the whole list, e-mails included (#1595). The
        # admitted identity is the allowlist row's ``(provider, subject_id)``;
        # the account it became is whatever ``user_oauth_providers`` maps that
        # identity to — the key ``RoleManager.ensure_user`` resolves a login by
        # (#517/#938). Both link columns are unique, so the joins cannot fan out.
        # The e-mail is read from the LIVE ``users`` row and deliberately never
        # from the allowlist ``subject_label`` snapshot: once the account is
        # erased (or the allowlist row pruned) the joins come up empty and the
        # address is gone from the inviter's view too.
        rows = (
            await self.db.execute(
                select(BetaInvite, User.email)
                .outerjoin(
                    SignupAllowlistEntry,
                    SignupAllowlistEntry.id == BetaInvite.redeemed_allowlist_entry_id,
                )
                .outerjoin(
                    UserOAuthProvider,
                    and_(
                        UserOAuthProvider.provider == SignupAllowlistEntry.provider,
                        UserOAuthProvider.oauth_sub == SignupAllowlistEntry.subject_id,
                    ),
                )
                .outerjoin(User, User.user_id == UserOAuthProvider.user_id)
                .where(BetaInvite.inviter_user_id == user_id)
                .order_by(BetaInvite.created_at.desc())
            )
        ).all()
        invites = [invite for invite, _email in rows]
        redeemed_emails = {
            invite.id: email
            for invite, email in rows
            if email is not None and invite.status_at(now) == "redeemed"
        }

        quota = self._quota_for(role)
        return BetaInviteSummary(
            quota=quota,
            used=used,
            active=active,
            redeemed=redeemed,
            remaining=None if quota is None else max(0, quota - used),
            invites=invites,
            redeemed_emails=redeemed_emails,
        )

    async def create(
        self,
        *,
        user_id: str,
        user_email: str,
        label: str | None = None,
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
            label: Optional inviter-private note, already normalised
                (:func:`normalize_beta_invite_label`). Stored on the invite row
                only — never logged or audited.
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
        await self._require_free_slot(role=role, user_id=user_id, now=now)

        invite, token = self._mint(user_id=user_id, label=label, now=now)
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
        revoked, _label = await self._revoke_unused(
            user_id=user_id, invite_id=invite_id, now=utcnow()
        )
        if revoked:
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
        if await self._why_not_revoked(user_id=user_id, invite_id=invite_id) == "redeemed":
            raise BetaInviteAlreadyRedeemedError()
        # Already revoked: a retry or a double-click. Nothing to do, no new audit row.

    async def reissue(
        self,
        *,
        user_id: str,
        invite_id: uuid.UUID,
        user_email: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> MintedBetaInvite:
        """Replace one of the caller's unused invites with a fresh link (#1595).

        For a recipient who lost the link: the stored hash cannot be turned back
        into a URL, so the old invite is revoked and a new one minted with the
        same label — in ONE transaction, in this order:

        1. row-lock the inviter (the lock :meth:`create` takes), which also
           serializes two reissues of the same invite;
        2. the guarded revoke (:func:`build_revoke_update`) must hit one row;
        3. the quota check. Reissuing an ``active`` invite always passes — its
           slot was freed one statement ago. An ``expired`` invite held no slot,
           so for it the check is real;
        4. mint the replacement, carrying the label over;
        5. two audit rows cross-referencing the two invites by ``id`` only;
        6. a single commit.

        Anything raised after the lock rolls the transaction back, so a refused
        reissue never leaves the old invite revoked.

        Args:
            user_id: The caller's OAuth ``sub``.
            invite_id: The ``active`` or ``expired`` invite to replace.
            user_email: The caller's e-mail, for the audit rows' actor column.
            ip_address: Caller IP for the audit rows.
            user_agent: Caller User-Agent for the audit rows.

        Returns:
            The new invite and its plaintext URL — the only time the URL exists.

        Raises:
            NotFoundException: No such invite, or it belongs to someone else (the
                same answer, so ids cannot be probed); or the caller's ``users``
                row is missing.
            BetaInviteAlreadyRedeemedError: The invite was already used.
            BetaInviteAlreadyRevokedError: The invite was already revoked — a
                double-click must not mint a second link.
            BetaInviteQuotaExceededError: An expired invite, and the caller is at
                the cap.
        """
        try:
            role = await self._lock_inviter_role(user_id)
            now = utcnow()
            revoked, label = await self._revoke_unused(
                user_id=user_id, invite_id=invite_id, now=now
            )
            if not revoked:
                if await self._why_not_revoked(user_id=user_id, invite_id=invite_id) == "redeemed":
                    raise BetaInviteAlreadyRedeemedError(
                        "This invite has already been used and cannot be reissued."
                    )
                raise BetaInviteAlreadyRevokedError()
            await self._require_free_slot(role=role, user_id=user_id, now=now)

            invite, token = self._mint(user_id=user_id, label=label, now=now)
            cross_refs = {"reissued_from": str(invite_id), "reissued_to": str(invite.id)}
            self.db.add(
                self._audit(
                    action="beta_invite.revoked",
                    invite_id=invite_id,
                    user_email=user_email,
                    user_id=user_id,
                    user_metadata=dict(cross_refs),
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )
            self.db.add(
                self._audit(
                    action="beta_invite.created",
                    invite_id=invite.id,
                    user_email=user_email,
                    user_id=user_id,
                    user_metadata={"expires_at": to_utc_iso(invite.expires_at), **cross_refs},
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

        logger.info(
            "beta_invite_reissued",
            invite_id=str(invite.id),
            reissued_from=str(invite_id),
            inviter_user_id=user_id,
        )
        return MintedBetaInvite(invite=invite, url=build_beta_invite_url(token))

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

    async def _count_slots(self, user_id: str, now: datetime) -> tuple[int, int]:
        """Return ``(active, redeemed)`` — see :func:`build_slot_counts_select`."""
        row = (await self.db.execute(build_slot_counts_select(user_id=user_id, now=now))).one()
        return int(row.active), int(row.redeemed)

    async def _count_used(self, user_id: str, now: datetime) -> int:
        """Count the invites occupying a quota slot: active + redeemed.

        Expired-unused and revoked invites free their slot.
        """
        active, redeemed = await self._count_slots(user_id, now)
        return active + redeemed

    async def _require_free_slot(self, *, role: str, user_id: str, now: datetime) -> None:
        """Raise ``BETA-INVITE-001`` when a capped inviter has no slot left.

        Call with the inviter row lock held, or two transactions can both see
        "one slot left".
        """
        quota = self._quota_for(role)
        if quota is None:
            return
        used = await self._count_used(user_id, now)
        if used >= quota:
            logger.info("beta_invite_quota_exceeded", inviter_user_id=user_id, used=used)
            raise BetaInviteQuotaExceededError(quota=quota)

    def _mint(self, *, user_id: str, label: str | None, now: datetime) -> tuple[BetaInvite, str]:
        """Add a fresh invite to the session; return it with its plaintext token.

        No quota check, no audit row, no commit — the caller owns all three.
        """
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        invite = BetaInvite(
            # Client-side id so the audit row can name the invite before flush.
            id=uuid.uuid4(),
            token_hash=sha256_hex(token),
            inviter_user_id=user_id,
            created_at=now,
            expires_at=now + BETA_INVITE_TTL,
            label=label,
        )
        self.db.add(invite)
        return invite, token

    async def _revoke_unused(
        self, *, user_id: str, invite_id: uuid.UUID, now: datetime
    ) -> tuple[bool, str | None]:
        """Run the guarded revoke. Does not commit — the caller owns the tx.

        Returns:
            ``(True, label)`` iff exactly one row was revoked (``label`` may be
            ``None``); ``(False, None)`` when the guard matched nothing.
        """
        rows = (
            await self.db.execute(
                build_revoke_update(invite_id=invite_id, user_id=user_id, now=now)
            )
        ).all()
        if len(rows) != 1:
            return False, None
        return True, rows[0].label

    async def _why_not_revoked(self, *, user_id: str, invite_id: uuid.UUID) -> str:
        """Explain a guarded revoke that matched nothing: ``redeemed`` or ``revoked``.

        Raises:
            NotFoundException: No such invite, or it belongs to someone else —
                the same answer, so ids cannot be probed.
        """
        invite = await self.db.scalar(
            select(BetaInvite).where(
                BetaInvite.id == invite_id, BetaInvite.inviter_user_id == user_id
            )
        )
        if invite is None:
            raise NotFoundException("Beta invite")
        return "redeemed" if invite.redeemed_at is not None else "revoked"

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
