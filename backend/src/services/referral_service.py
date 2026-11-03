"""Referral program service (Issue #1470).

Design contract, in one paragraph: ``referral_grants`` is the ledger and
``Workspace.referral_memory_bonus`` is a derived cache. The cache is **never
incremented** — it is recomputed as the absolute SUM of non-revoked ledger rows
for that workspace. That single rule buys three properties at once:

- **Idempotent.** Running the recompute twice, or in either order across the two
  affected workspaces, converges on the same value.
- **Reversible.** Revoke sets ``revoked_at`` and recomputes; the bonus drops by
  exactly what that row contributed, never by whatever the config happens to say
  today.
- **Non-retroactive.** The applied amounts are snapshotted onto the row at grant
  time, so lowering ``settings.referral_referee_reward_memories`` cannot
  retro-shrink a limit a user is already storing against.

The same rule is why the reward does not live in ``ADDON_UNIT_VALUES``: that
constant *is* a re-valuing multiplier, and changing it would silently re-price
every existing grant on the next recalc.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import User, Workspace
from models.referral import ReferralGrant
from services.workspace_locks import retry_nowait
from utils.datetime import utcnow
from utils.exceptions import (
    ConflictError,
    NotFoundException,
    ReferralAlreadyRedeemedError,
    ReferralCapReachedError,
    ReferralCodeInvalidError,
    ReferralSelfError,
    ReferralWindowClosedError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# 12 bytes of entropy -> 16 URL-safe characters, comfortably inside the
# VARCHAR(24) column. Sized to be pasteable and to make guessing a live code
# pointless (2^96), since a valid code is the only thing standing between an
# attacker and knowing that some account exists.
_REFERRAL_CODE_BYTES = 12
_MAX_CODE_MINT_ATTEMPTS = 5

# The two sides of a grant: a user and the workspace credited on their side.
_GRANT_SIDES = (
    (ReferralGrant.referrer_user_id, ReferralGrant.referrer_workspace_id),
    (ReferralGrant.referred_user_id, ReferralGrant.referred_workspace_id),
)


def _uncredited(workspace_col: Any) -> Any:
    """The side ``workspace_col`` credits no live workspace: unset, or soft-deleted."""
    # Workspaces a grant may currently point at that no longer count. A subquery
    # keeps this a single statement.
    dead_workspaces = select(Workspace.id).where(Workspace.deleted_at.is_not(None))
    return workspace_col.is_(None) | workspace_col.in_(dead_workspaces)


def build_pending_grant_update(
    user_col: Any,
    workspace_col: Any,
    user_id: str,
    workspace_id: uuid.UUID,
) -> Any:
    """Build the UPDATE that re-homes one side of a user's unresolved grants.

    Extracted from :meth:`ReferralService.apply_pending_grants` so the emitted
    SQL is assertable without a live database — see
    ``tests/api/test_referrals.py::TestPendingGrantSQL``. The predicate mixes
    three-valued logic in a way that is easy to get wrong and that only fails at
    runtime, which is exactly the class of defect a compile-time assertion
    catches cheaply.

    Selects grants for ``user_id`` whose workspace on this side is either unset
    or points at a soft-deleted workspace, and is not already the target.

    Args:
        user_col: ``referrer_user_id`` or ``referred_user_id``.
        workspace_col: The matching ``*_workspace_id`` column.
        user_id: Owner of the grants being re-homed.
        workspace_id: The live workspace to point them at.

    Returns:
        A SQLAlchemy ``Update`` statement.
    """
    return (
        update(ReferralGrant)
        .where(
            user_col == user_id,
            _uncredited(workspace_col),
            # NULL-safe inequality, and it has to be. ``!=`` evaluates to NULL
            # (not TRUE) for exactly the NULL rows this UPDATE exists to fix, so
            # it would silently match nothing. ``is_not`` emits SQL
            # ``IS NOT <uuid>``, which PostgreSQL accepts only for
            # NULL / TRUE / FALSE / UNKNOWN — a syntax error against a UUID.
            workspace_col.is_distinct_from(workspace_id),
            ReferralGrant.revoked_at.is_(None),
        )
        .values({workspace_col.key: workspace_id})
    )


def _in_lock_order(workspace_ids: tuple[uuid.UUID | None, ...]) -> list[uuid.UUID]:
    """The workspaces to lock, de-duplicated and UUID-ordered (see
    :meth:`ReferralService._recompute_workspace_bonuses`)."""
    return sorted({w for w in workspace_ids if w is not None})


@dataclass(frozen=True)
class ReferralSummary:
    """What a user sees about their own referral standing."""

    code: str
    max_grants: int
    used_grants: int
    referee_reward_memories: int
    referrer_reward_memories: int
    earned_memories: int


class ReferralService:
    """Mint referral codes, redeem them idempotently, and revoke grants."""

    def __init__(self, db: AsyncSession):
        """Initialize.

        Args:
            db: Async database session.
        """
        self.db = db
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # Codes
    # ------------------------------------------------------------------

    async def get_or_create_referral_code(self, user_id: str) -> str:
        """Return the user's referral code, minting one on first use.

        Lazily minted rather than backfilled so the migration stays additive and
        users who never open the invite page never get a row update.

        Args:
            user_id: The user's OAuth ``sub``.

        Returns:
            The user's referral code.

        Raises:
            NotFoundException: If the user row does not exist.
            RuntimeError: If a unique code could not be minted (astronomically
                unlikely; surfaced rather than looped forever).
        """
        user = await self.db.scalar(select(User).where(User.user_id == user_id))
        if user is None:
            # 404, not a REFERRAL-* refusal. The caller is session-authenticated
            # and submitted no code — a missing ``users`` row is a
            # resource-not-found condition (an erased account with a live
            # session), and answering "this referral code is not valid" would be
            # a lie about a code that was never sent. Keeps the REFERRAL-* codes
            # meaning "your redemption was refused".
            raise NotFoundException("User", user_id)
        if user.referral_code:
            return user.referral_code

        # Retry on the UNIQUE index rather than pre-checking: the pre-check
        # would be racy anyway, and the index is the authority.
        #
        # Each attempt runs inside a SAVEPOINT. Without one, a unique collision
        # aborts the whole transaction and every subsequent statement fails with
        # InFailedSQLTransactionError — i.e. the loop would look like it retries
        # but could not. ``begin_nested`` releases the savepoint on success and
        # rolls back only to it on IntegrityError, leaving the outer transaction
        # (which may belong to the caller) usable.
        for _ in range(_MAX_CODE_MINT_ATTEMPTS):
            candidate = secrets.token_urlsafe(_REFERRAL_CODE_BYTES)
            try:
                async with self.db.begin_nested():
                    result = await self.db.execute(
                        update(User)
                        .where(User.user_id == user_id, User.referral_code.is_(None))
                        .values(referral_code=candidate)
                        .returning(User.referral_code)
                    )
                    minted = result.scalar_one_or_none()
            except IntegrityError:
                # Candidate collided with an existing code (2^96 odds) — try again.
                continue

            if minted:
                await self.db.commit()
                return minted

            # Zero rows updated => the ``referral_code IS NULL`` predicate failed,
            # i.e. a concurrent request already minted one. Re-read and use it.
            existing = await self.db.scalar(
                select(User.referral_code).where(User.user_id == user_id)
            )
            if existing:
                return existing

        raise RuntimeError(f"Could not mint a unique referral code for user {user_id}")

    # ------------------------------------------------------------------
    # Redemption
    # ------------------------------------------------------------------

    async def redeem(self, *, referred_user_id: str, code: str) -> ReferralGrant:
        """Redeem ``code`` on behalf of ``referred_user_id`` and pay both sides.

        The whole method is a single transaction. The cap check runs under row
        locks on the referrer and the referee, taken in one ``user_id``-ordered
        statement, so two concurrent redemptions of the same code cannot both
        observe "one slot left" and two users redeeming each other's codes
        cannot deadlock (#2573); the payout itself is guarded by
        ``uq_referral_grants_referred_user`` so a retry pays zero regardless.

        Args:
            referred_user_id: The redeeming (invited) user's OAuth ``sub``.
            code: The referral code they were given.

        Returns:
            The created :class:`ReferralGrant`.

        Raises:
            NotFoundException: The redeeming user's own row is missing.
            ReferralCodeInvalidError: Code does not resolve to a user.
            ReferralSelfError: The code belongs to the redeeming user.
            ReferralWindowClosedError: The account is older than the redeem window.
            ReferralCapReachedError: The referrer has no slots left.
            ReferralAlreadyRedeemedError: This user was already referred.
            ConflictError: A workspace row to credit is held by another
                transaction past the ``NOWAIT`` tries (409); nothing is paid.
        """
        referee = await self.db.scalar(select(User).where(User.user_id == referred_user_id))
        if referee is None:
            # See get_or_create_referral_code: the redeemer's own missing row is
            # 404, independent of whatever code they submitted. It leaks nothing
            # about the code — it is a fact about the caller.
            raise NotFoundException("User", referred_user_id)

        # Caller-state checks FIRST, before the code is ever looked up. Ordering
        # is load-bearing for the enumeration story: if the window check ran
        # after code resolution, receiving REFERRAL-004 rather than -001 would
        # itself prove the submitted code exists.
        self._assert_within_redeem_window(referee)

        normalized = (code or "").strip()
        if not normalized:
            raise ReferralCodeInvalidError()

        referrer_id = await self.db.scalar(
            select(User.user_id).where(User.referral_code == normalized)
        )
        if referrer_id is None:
            raise ReferralCodeInvalidError()
        if referrer_id == referred_user_id:
            raise ReferralSelfError()

        # Lock both users rows for the duration of the cap check + insert: the
        # referrer's, so two invitees redeeming the same code cannot both see
        # the last free slot, and the referee's, which the INSERT's foreign key
        # would otherwise take later (a KEY SHARE). A transaction that needs
        # two users rows locks both in user_id order in one statement (#2573):
        # with the referrer's row alone, A redeeming B's code while B redeems
        # A's each held one row and waited on the other's (a deadlock). No
        # NOWAIT: this transaction holds nothing yet. FOR NO KEY UPDATE, the
        # weakest lock that serializes two redeems and still waits on an
        # erasure's FOR UPDATE / DELETE: it does not conflict with the FOR KEY
        # SHARE a remember, a foreign-key check or a context restore takes on
        # a users row. The restore takes its authors' rows in users.id order,
        # so against FOR UPDATE it could hold one of the two rows and wait on
        # the other (a deadlock). The users rows come before any workspace
        # lock: the global lock order
        # (tests/integration/test_erasure_lock_order_db.py) is users rows, then
        # workspace rows, and a transaction holding a users row never waits on
        # a workspace row. ``DELETE /workspaces/W`` locks W first and then the
        # users rows it clears, so a wait on W here could close that cycle; the
        # workspace rows below are taken NOWAIT, UUID-ordered (#2443).
        locked = set(
            (
                await self.db.execute(
                    select(User.user_id)
                    .where(User.user_id.in_([referrer_id, referred_user_id]))
                    .order_by(User.user_id)
                    .with_for_update(key_share=True)
                )
            ).scalars()
        )
        # A row missing here was deleted (an account erasure) while the lock
        # waited on it: the answers of the reads above, not a foreign-key
        # failure at the INSERT.
        if referred_user_id not in locked:
            raise NotFoundException("User", referred_user_id)
        if referrer_id not in locked:
            raise ReferralCodeInvalidError()

        used = await self._count_active_grants(referrer_id)
        if used >= self.settings.referral_max_grants_per_referrer:
            # Logged with the true reason; the client sees REFERRAL-001, the
            # same code an unknown referral code returns.
            logger.info(
                "referral_cap_reached",
                referrer_user_id=referrer_id,
                referred_user_id=referred_user_id,
                used=used,
            )
            raise ReferralCapReachedError()

        referrer_workspace_id = await self._resolve_owned_workspace_id(referrer_id)
        referred_workspace_id = await self._resolve_owned_workspace_id(referred_user_id)

        # Both workspace rows before the INSERT: its foreign keys take a KEY
        # SHARE on them, which waits on a row another transaction holds FOR
        # UPDATE. Held by this transaction, they make that check and the
        # recompute's re-lock free. A row still busy after the tries is a
        # ConflictError (409) with nothing written: the client retries.
        await self._lock_workspaces_nowait(referrer_workspace_id, referred_workspace_id)

        stmt = (
            pg_insert(ReferralGrant)
            .values(
                referrer_user_id=referrer_id,
                referrer_workspace_id=referrer_workspace_id,
                referred_user_id=referred_user_id,
                referred_workspace_id=referred_workspace_id,
                referrer_bonus_memories=self.settings.referral_referrer_reward_memories,
                referred_bonus_memories=self.settings.referral_referee_reward_memories,
                granted_at=utcnow(),
            )
            .on_conflict_do_nothing(constraint="uq_referral_grants_referred_user")
            .returning(ReferralGrant.id)
        )
        grant_id = await self.db.scalar(stmt)
        if grant_id is None:
            # Zero rows inserted => this user already has a grant. Nothing was
            # paid, so there is nothing to roll back.
            raise ReferralAlreadyRedeemedError()

        await self._recompute_workspace_bonuses(referrer_workspace_id, referred_workspace_id)

        await self.db.commit()

        logger.info(
            "referral_redeemed",
            grant_id=str(grant_id),
            referrer_user_id=referrer_id,
            referred_user_id=referred_user_id,
            referrer_workspace_id=str(referrer_workspace_id) if referrer_workspace_id else None,
            referred_workspace_id=str(referred_workspace_id) if referred_workspace_id else None,
        )

        grant = await self.db.get(ReferralGrant, grant_id)
        if grant is None:  # pragma: no cover - just inserted in this transaction
            raise ReferralAlreadyRedeemedError()
        return grant

    def _assert_within_redeem_window(self, referee: User) -> None:
        """Reject redemption by an account older than the configured window.

        This is the "is this genuinely a new user" check. It is deliberately
        based on ``users.created_at`` rather than on any behavioural signal:
        it is observable with no extra query, and it stops an established
        account from being farmed for a code long after signup.
        """
        window_hours = self.settings.referral_redeem_window_hours
        if referee.created_at is None:
            return
        if utcnow() - referee.created_at > timedelta(hours=window_hours):
            raise ReferralWindowClosedError(window_hours=window_hours)

    async def _count_active_grants(self, referrer_user_id: str) -> int:
        """Count non-revoked grants attributed to a referrer."""
        return (
            await self.db.scalar(
                select(func.count(ReferralGrant.id)).where(
                    ReferralGrant.referrer_user_id == referrer_user_id,
                    ReferralGrant.revoked_at.is_(None),
                )
            )
            or 0
        )

    async def _resolve_owned_workspace_id(self, user_id: str) -> uuid.UUID | None:
        """Return the user's oldest live OWNED workspace, or ``None``.

        Deliberately not ``users.current_workspace_id``: that column is mutable
        and can point at a workspace the user merely belongs to, which would
        gift quota to somebody else's workspace.

        ``None`` is a legitimate outcome, not an error. ``api/routes/auth.py``
        skips personal-workspace creation entirely when the user has pending
        team invitations, so a brand-new referee may genuinely own nothing yet.
        The grant is still recorded; it is applied by
        :meth:`apply_pending_grants` once a workspace exists.
        """
        return await self.db.scalar(
            select(Workspace.id)
            .where(
                Workspace.owner_user_id == user_id,
                Workspace.deleted_at.is_(None),
            )
            .order_by(Workspace.created_at.asc())
            .limit(1)
        )

    async def _lock_workspaces_nowait(self, *workspace_ids: uuid.UUID | None) -> None:
        """Lock workspace rows ``NOWAIT`` in the recompute's order (#2443).

        Raises:
            ConflictError: One is still held by another transaction after the
                tries (409).
        """
        for workspace_id in _in_lock_order(workspace_ids):
            await self._lock_workspace_nowait(workspace_id)

    async def _recompute_workspace_bonuses(self, *workspace_ids: uuid.UUID | None) -> None:
        """Recompute several workspaces in one UUID-ordered, de-duplicated pass.

        Callers pass workspaces by ROLE (referrer, then referee), so mutual
        referrals pass them in opposite orders: A-invites-B (Wa, Wb) while
        B-invites-A (Wb, Wa). Sorting by UUID gives a total order shared by
        every transaction in the system: the one that gets the first row also
        gets the second, instead of each holding one and losing its ``NOWAIT``
        tries on the other. De-duplication avoids locking (and recomputing) the
        same workspace twice when both sides resolve to one workspace —
        reachable after an ownership transfer.

        Raises:
            ConflictError: See :meth:`_recompute_workspace_bonus`.
        """
        for workspace_id in _in_lock_order(workspace_ids):
            await self._recompute_workspace_bonus(workspace_id)

    async def _recompute_workspace_bonus(self, workspace_id: uuid.UUID | None) -> None:
        """Rewrite ``referral_memory_bonus`` as the absolute SUM of live grants.

        Absolute, never incremental — see the module docstring. A workspace with
        no grants converges to 0, which is also how revocation takes effect.

        The SELECT-then-UPDATE pair is not atomic on its own: under READ
        COMMITTED the UPDATE's row lock serializes the write but not the SUM, so
        two concurrent recomputes would each stamp a value computed from their
        own pre-commit snapshot and the later commit would silently drop the
        other's ledger change. Locking the workspace row first makes the read and
        the write one critical section.

        The lock is ``NOWAIT``, tried again for a moment
        (:meth:`_lock_workspace_nowait`): ``redeem`` holds two users rows
        here, and a transaction holding a users row never waits on a
        workspace row (#2443). ``revoke`` and ``apply_pending_grants`` hold none
        and get the same behaviour. Prefer ``_recompute_workspace_bonuses`` over
        calling this directly with more than one workspace — that wrapper
        supplies the order the locks are taken in.

        Raises:
            ConflictError: Another transaction still holds the workspace row
                after the tries (409). The caller's transaction must be rolled
                back; nothing of it is committed.
        """
        if workspace_id is None:
            return

        await self._lock_workspace_nowait(workspace_id)

        referrer_total = func.coalesce(
            func.sum(ReferralGrant.referrer_bonus_memories).filter(
                ReferralGrant.referrer_workspace_id == workspace_id
            ),
            0,
        )
        referred_total = func.coalesce(
            func.sum(ReferralGrant.referred_bonus_memories).filter(
                ReferralGrant.referred_workspace_id == workspace_id
            ),
            0,
        )
        total = await self.db.scalar(
            select(referrer_total + referred_total).where(
                ReferralGrant.revoked_at.is_(None),
                (ReferralGrant.referrer_workspace_id == workspace_id)
                | (ReferralGrant.referred_workspace_id == workspace_id),
            )
        )
        await self.db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(referral_memory_bonus=int(total or 0))
        )

    async def _lock_workspace_nowait(self, workspace_id: uuid.UUID) -> None:
        """``SELECT ... FOR UPDATE NOWAIT`` the workspace row, retried (#2443).

        Raises:
            ConflictError: Another transaction still holds the row after the
                tries (409).
        """
        await retry_nowait(
            self.db,
            lambda: self.db.execute(
                select(Workspace.id)
                .where(Workspace.id == workspace_id)
                .with_for_update(nowait=True)
            ),
        )

    async def apply_pending_grants(self, user_id: str) -> int:
        """Attach a workspace to this user's grants that were recorded without one.

        Called after workspace creation, when the user reads the referral
        summary, and by the scheduler sweep
        (``tasks.referral_tasks``, #2563) — the retry point for grants whose
        workspace row was busy the first time. Two states are re-homed here:

        - grants earned while the user owned NO workspace (pending team
          invitations suppress personal-workspace bootstrap), and
        - grants pointing at a workspace the user has since SOFT-DELETED.

        The second case matters because the bonus rides on a specific workspace
        row: without re-homing, deleting that workspace strands the grant
        forever while ``get_summary`` keeps reporting it as earned — the user
        sees a bonus they no longer have anywhere.

        The caller holds no row lock (in particular no ``users`` row). When a
        grant is pending this takes the workspace row ``NOWAIT``, then the
        grant rows it re-homes — the global order's workspace rows before
        child rows — and never waits on the workspace row (#2443, #2563).

        Args:
            user_id: The user whose grants should be re-resolved.

        Returns:
            Number of grant rows updated; committed when not 0.

        Raises:
            ConflictError: The workspace row stays busy past the ``NOWAIT``
                tries (409). Everything since the pending check is rolled back
                to a savepoint: the grants stay pending, and the caller's
                transaction holds nothing of it and stays usable.
        """
        workspace_id = await self._resolve_owned_workspace_id(user_id)
        if workspace_id is None or not await self._has_pending_grants(user_id):
            return 0

        async with self.db.begin_nested() as savepoint:
            # The workspace row first: the re-homing UPDATEs' foreign-key
            # checks take a KEY SHARE on it, which waits on a transaction
            # holding it FOR UPDATE. Held here, those checks and the
            # recompute's re-lock are free.
            await self._lock_workspace_nowait(workspace_id)
            # It was picked by a plain read. An ownership transfer or a delete
            # that committed since (one that held the row while the tries ran)
            # has moved it on: re-homed onto it, the bonus would go to someone
            # else's workspace. The grants stay pending for the next apply.
            if not await self._owns_live_workspace(user_id, workspace_id):
                await savepoint.rollback()
                return 0
            updated = 0
            for user_col, workspace_col in _GRANT_SIDES:
                result = await self.db.execute(
                    build_pending_grant_update(user_col, workspace_col, user_id, workspace_id)
                )
                # ``AsyncSession.execute`` is typed as returning ``Result``, which
                # has no ``rowcount``; a DML statement always yields a
                # ``CursorResult`` at runtime. Narrow explicitly rather than
                # suppressing the diagnostic.
                updated += cast(CursorResult[Any], result).rowcount or 0
            if not updated:
                # Another transaction applied them since the check: let go of
                # the workspace row rather than hold it for the caller.
                await savepoint.rollback()
                return 0
            await self._recompute_workspace_bonus(workspace_id)

        await self.db.commit()
        logger.info(
            "referral_pending_grants_applied",
            user_id=user_id,
            workspace_id=str(workspace_id),
            grants_applied=updated,
        )
        return updated

    async def _owns_live_workspace(self, user_id: str, workspace_id: uuid.UUID) -> bool:
        """Whether ``user_id`` still owns ``workspace_id`` and it is live."""
        return (
            await self.db.scalar(
                select(Workspace.id).where(
                    Workspace.id == workspace_id,
                    Workspace.owner_user_id == user_id,
                    Workspace.deleted_at.is_(None),
                )
            )
            is not None
        )

    async def _has_pending_grants(self, user_id: str) -> bool:
        """Whether a live grant credits no live workspace on ``user_id``'s side.

        A plain read, no lock: it only spares :meth:`apply_pending_grants`
        the workspace lock when there is nothing to re-home.
        """
        return bool(
            await self.db.scalar(
                select(
                    select(ReferralGrant.id)
                    .where(
                        ReferralGrant.revoked_at.is_(None),
                        or_(
                            *(
                                (user_col == user_id) & _uncredited(workspace_col)
                                for user_col, workspace_col in _GRANT_SIDES
                            )
                        ),
                    )
                    .exists()
                )
            )
        )

    async def users_with_pending_grants(self, *, limit: int) -> list[str]:
        """Users with a pending grant who own a live workspace, oldest grant first.

        Pending is what :meth:`apply_pending_grants` re-homes: a grant that is
        not revoked and credits no live workspace on the user's side. A user
        who owns no live workspace has nothing to credit yet and is left out,
        so every user returned is one an apply would change (#2563).

        Args:
            limit: The most users to return.

        Returns:
            User ids, the one with the oldest pending grant first.
        """
        sides = [
            select(user_col.label("user_id"), ReferralGrant.granted_at.label("granted_at")).where(
                ReferralGrant.revoked_at.is_(None),
                _uncredited(workspace_col),
                select(Workspace.id)
                .where(Workspace.owner_user_id == user_col, Workspace.deleted_at.is_(None))
                .exists(),
            )
            for user_col, workspace_col in _GRANT_SIDES
        ]
        pending = union_all(*sides).subquery()
        return list(
            await self.db.scalars(
                select(pending.c.user_id)
                .group_by(pending.c.user_id)
                .order_by(func.min(pending.c.granted_at), pending.c.user_id)
                .limit(limit)
            )
        )

    # ------------------------------------------------------------------
    # Read models
    # ------------------------------------------------------------------

    async def get_summary(self, user_id: str) -> ReferralSummary:
        """Build the user-facing referral summary, minting a code if needed.

        Also applies the user's pending grants (#2563), so a bonus whose
        workspace row was busy when it was earned is credited by the time the
        user looks. It runs after the code is minted — the mint commits on its
        own — so it starts with no row lock held. A workspace row still busy
        after the tries leaves the grants to the scheduler sweep; the summary
        renders either way and does not depend on them.
        """
        code = await self.get_or_create_referral_code(user_id)
        try:
            await self.apply_pending_grants(user_id)
        except ConflictError:
            logger.info("referral_pending_grants_busy", user_id=user_id)
        used = await self._count_active_grants(user_id)
        earned = (
            await self.db.scalar(
                select(func.coalesce(func.sum(ReferralGrant.referrer_bonus_memories), 0)).where(
                    ReferralGrant.referrer_user_id == user_id,
                    ReferralGrant.revoked_at.is_(None),
                )
            )
            or 0
        )
        return ReferralSummary(
            code=code,
            max_grants=self.settings.referral_max_grants_per_referrer,
            used_grants=used,
            referee_reward_memories=self.settings.referral_referee_reward_memories,
            referrer_reward_memories=self.settings.referral_referrer_reward_memories,
            earned_memories=int(earned),
        )

    async def list_grants(
        self,
        *,
        referrer_user_id: str | None = None,
        include_revoked: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ReferralGrant], int]:
        """List ledger rows, newest first, with a total count.

        Args:
            referrer_user_id: Restrict to one referrer (the admin filter).
            include_revoked: When False, hide revoked rows.
            limit: Page size.
            offset: Page offset.

        Returns:
            ``(rows, total)``.
        """
        filters = []
        if referrer_user_id is not None:
            filters.append(ReferralGrant.referrer_user_id == referrer_user_id)
        if not include_revoked:
            filters.append(ReferralGrant.revoked_at.is_(None))

        total = (await self.db.scalar(select(func.count(ReferralGrant.id)).where(*filters))) or 0
        rows = list(
            await self.db.scalars(
                select(ReferralGrant)
                .where(*filters)
                .order_by(ReferralGrant.granted_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        return rows, total

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    async def revoke(
        self, *, grant_id: uuid.UUID, reason: str
    ) -> tuple[ReferralGrant | None, bool]:
        """Revoke a grant and recompute both sides' bonuses.

        Idempotent: revoking an already-revoked grant leaves ``revoked_at`` and
        the original ``revoked_reason`` untouched, so an admin retry (or a
        double-click) changes nothing.

        The ``changed`` flag exists because "idempotent" has to cover the side
        effects too. Callers that write an audit row must key off it: auditing
        unconditionally would stamp a *new* reason onto a no-op, producing an
        audit trail that disagrees with the ``revoked_reason`` actually stored on
        the row — the log would claim a revocation happened for a reason that was
        never recorded anywhere.

        Args:
            grant_id: The ledger row to revoke.
            reason: Required free text; recorded on the row and in the audit log.

        Returns:
            ``(grant, changed)``. ``grant`` is ``None`` if no such row exists;
            ``changed`` is True only when this call performed the revocation.

        Raises:
            ConflictError: A workspace row stays busy past the ``NOWAIT`` tries
                (409); the caller rolls back the revocation and retries.
        """
        grant = await self.db.get(ReferralGrant, grant_id)
        if grant is None:
            return None, False

        if grant.revoked_at is not None:
            return grant, False

        grant.revoked_at = utcnow()
        grant.revoked_reason = reason
        await self.db.flush()
        await self._recompute_workspace_bonuses(
            grant.referrer_workspace_id, grant.referred_workspace_id
        )
        logger.info(
            "referral_revoked",
            grant_id=str(grant_id),
            referrer_user_id=grant.referrer_user_id,
            referred_user_id=grant.referred_user_id,
        )
        return grant, True
