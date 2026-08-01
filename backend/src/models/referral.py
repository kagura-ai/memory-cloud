"""Referral program models (Issue #1470).

A referral is user-scoped and deliberately separate from ``WorkspaceInvitation``
(``models/auth.py``), which is a Pro-only *team member* invite requiring at least
one shared context. Free users cannot send those at all
(``plan_tiers.py`` ``max_members_per_workspace == 1`` on FREE/BASIC), which is
precisely the audience a growth referral has to reach.

``ReferralGrant`` is the **ledger** — the authoritative record of every payout.
``Workspace.referral_memory_bonus`` is a derived cache recomputed as the absolute
SUM over this table's non-revoked rows, never incremented in place. That contract
is what makes the grant idempotent (a retried redemption converges on the same
value) and reversible (revoke + recompute subtracts exactly what was added).

The applied amounts are **snapshotted per row** rather than read from config at
display time. This is what decouples the tunable reward from history: lowering
``settings.referral_referee_reward_memories`` can never retro-shrink a limit a
user is already storing against, and a revoke always subtracts the amount that
was actually granted.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Kept in lockstep with the alembic e76_1470 migration literals. tests/test_schema_drift.py
# compares the SQL text of every named CheckConstraint against the migration's, so these
# strings must stay byte-identical to the ones in the migration file.
CK_NOT_SELF = (
    "referrer_user_id IS NULL OR referred_user_id IS NULL OR referrer_user_id <> referred_user_id"
)
CK_BONUS_NONNEG = "referrer_bonus_memories >= 0 AND referred_bonus_memories >= 0"


class ReferralGrant(Base):
    """One successful referral: who referred whom, and what each side was paid.

    Idempotency is enforced by the database, not by application logic:
    ``uq_referral_grants_referred_user`` means a given user can be referred at
    most once, ever. The redemption path is a single
    ``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` — a retried request, a
    duplicated submit, or two concurrent redemptions all insert zero rows and
    pay zero. PostgreSQL treats NULLs as distinct in a UNIQUE index, so rows
    whose ``referred_user_id`` has been NULLed by account erasure do not block
    anything.

    All four FKs are ``ON DELETE SET NULL`` on purpose:

    - Account erasure (``services/account_erasure_service.py``) enumerates the
      tables it sweeps explicitly. SET NULL means this table needs no new entry
      there — the row survives as an anonymous audit record.
    - Erasing an invitee must NOT retroactively void the referrer's earned
      quota. The cache is recomputed from non-revoked rows and the amounts are
      snapshotted on the row, so a NULLed counterparty leaves the payout intact.

    Attributes:
        id: Primary key (UUID).
        referrer_user_id: The inviter's user ID (OAuth sub). NULL after erasure.
        referrer_workspace_id: Workspace credited on the inviter's side. NULL
            means "earned but not yet applied" — see ``referred_workspace_id``.
        referred_user_id: The invitee's user ID. NULL after erasure. Carries the
            uniqueness constraint that makes payout idempotent.
        referred_workspace_id: Workspace credited on the invitee's side. NULL
            when no owned workspace could be resolved at redemption time —
            ``api/routes/auth.py`` skips personal-workspace creation entirely
            when a user has pending team invitations, so this genuinely happens.
            The grant is recorded anyway and applied on the next resolution.
        referrer_bonus_memories: Amount applied to the referrer, snapshotted.
        referred_bonus_memories: Amount applied to the referee, snapshotted.
        granted_at: When the referral was redeemed.
        revoked_at: Set by an admin revoke; excludes the row from the cache SUM.
        revoked_reason: Required free text supplied by the revoking admin.
    """

    __tablename__ = "referral_grants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    referrer_user_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    referrer_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    referred_user_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    referred_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    referrer_bonus_memories: Mapped[int] = mapped_column(Integer, nullable=False)
    referred_bonus_memories: Mapped[int] = mapped_column(Integer, nullable=False)

    granted_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # THE idempotency key. Postgres allows multiple NULLs in a UNIQUE index,
        # so erased referees never collide.
        UniqueConstraint("referred_user_id", name="uq_referral_grants_referred_user"),
        CheckConstraint(CK_NOT_SELF, name="ck_referral_grants_not_self"),
        CheckConstraint(CK_BONUS_NONNEG, name="ck_referral_grants_bonus_nonneg"),
        Index("ix_referral_grants_referrer_active", "referrer_user_id", "revoked_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ReferralGrant(id='{self.id}', referrer='{self.referrer_user_id}', "
            f"referred='{self.referred_user_id}')>"
        )
