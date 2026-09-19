"""Closed-beta invite link model (Issue #1581).

A beta invite is a one-time pass through the **platform signup gate**
(``models/signup_gate.py``). It is deliberately separate from
``WorkspaceInvitation`` (``models/auth.py``), which adds a user to one workspace
and is a different layer entirely (#358 disambiguation note), and from the
referral program (``models/referral.py``), which pays quota and admits nobody.

Only ``sha256_hex(token)`` is stored — the API-key pattern (``auth/api_keys.py``).
The plaintext exists in exactly one place: the ``POST /beta-invites`` response.

Status is **derived**, never stored, so it cannot disagree with the timestamps
that the atomic redeem ``UPDATE`` and the quota ``COUNT`` filter on.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import CHAR, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

# FK target of ``beta_invites.redeemed_allowlist_entry_id``. ``models/__init__``
# registers this module in every process, so the table it points at has to come
# along with it — otherwise ``Base.metadata`` is unresolvable
# (``NoReferencedTableError``) wherever nothing else imports the signup gate:
# alembic autogenerate, and the ``create_all`` in ``tests/conftest.py``, which
# turns the error into a silent skip of every DB-backed test.
import models.signup_gate  # noqa: F401
from db.base import Base
from utils.datetime import utcnow

BetaInviteStatus = Literal["active", "redeemed", "expired", "revoked"]


class BetaInvite(Base):
    """One minted invite link.

    Attributes:
        id: Primary key (UUID). The only identifier that may appear in logs and
            audit rows — never the token, its hash, or the URL.
        token_hash: ``sha256_hex`` of the URL token. Unique lookup key.
        inviter_user_id: The minting user's ID (OAuth sub). ``ON DELETE CASCADE``
            — account erasure removes the inviter's links.
        created_at: When the link was minted.
        expires_at: ``created_at`` + the fixed TTL.
        redeemed_at: Set once, by the signup gate's atomic redeem ``UPDATE``.
        redeemed_allowlist_entry_id: The ``signup_allowlist`` row written at
            redemption. ``ON DELETE SET NULL`` — an admin pruning that row does
            not un-redeem the invite. Never exposed to the inviter.
        revoked_at: Set when the inviter revokes an unused link.
    """

    __tablename__ = "beta_invites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    inviter_user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    redeemed_allowlist_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("signup_allowlist.id", ondelete="SET NULL"),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("token_hash", name="uq_beta_invites_token_hash"),)

    def status_at(self, now: datetime) -> BetaInviteStatus:
        """Derive the status as of ``now``: revoked > redeemed > expired > active.

        The single place the precedence lives. ``revoked`` wins over ``redeemed``
        only in theory — revoke refuses a redeemed invite and redeem refuses a
        revoked one — but a fixed order keeps a hand-edited row unambiguous.

        Args:
            now: Naive UTC instant to evaluate expiry against.

        Returns:
            The derived status.
        """
        if self.revoked_at is not None:
            return "revoked"
        if self.redeemed_at is not None:
            return "redeemed"
        if self.expires_at <= now:
            return "expired"
        return "active"

    @property
    def status(self) -> BetaInviteStatus:
        """The status right now (see :meth:`status_at`)."""
        return self.status_at(utcnow())

    def __repr__(self) -> str:
        return f"<BetaInvite(id='{self.id}', inviter='{self.inviter_user_id}')>"
