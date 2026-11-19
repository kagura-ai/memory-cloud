"""Workspace slot suspension state (Issue #2918).

An event that ends a paid contract, lowers a plan's owned-workspace grant or
lowers the slot bonus, and does not raise the allowance, suspends the owner's
excess owned workspaces over the allowance, with their data kept; they resume
when slots return. ``services.workspace_slots`` writes both tables: the reconcile
(the only writer of suspension rows outside the end-to-end seed and account
deletion), the owner's saved selection, and the erasure of an account.

- ``workspace_slot_states``: one row per owner, created lazily: the owner's
  ordered selection of workspaces to keep active, when it was saved, since when
  a selection is required, and a version the account page sends back.
- ``workspace_slot_suspensions``: a row exists = the workspace is suspended,
  stamped with the owner it was suspended for. It counts only while that user
  still owns the live workspace and the workspace is not under a billing
  contract, so a row left behind by an ownership move, a deletion or a mode
  switch is inert.

No foreign keys: an insert would take a key-share lock on a workspace row that a
concurrent billing delivery for a sibling workspace holds for update. Rows of a
deleted workspace or user are inert and pruned by the sweep; account erasure and
user deletion delete the subject's rows. Timestamps are naive UTC. No content,
no learned structure: operational plumbing.
"""

from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# The only suspension reason today; the CHECK keeps the column honest.
SLOT_SUSPENSION_REASON_ALLOWANCE: Final = "allowance"


class WorkspaceSlotState(Base):
    """One owner's selection and selection-required state (#2918)."""

    __tablename__ = "workspace_slot_states"

    owner_user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    # Ordered: the first ids are kept first. Intersected with the live owned
    # workspaces whenever it is used; may name more ids than there are slots.
    keep_workspace_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    selection_saved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set when a drop suspended workspaces the owner had not chosen; cleared
    # when they save a selection or nothing is suspended any more.
    selection_required_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Bumped whenever the selection or the active set changes (SLOT-003).
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class WorkspaceSlotSuspension(Base):
    """A suspended workspace and the owner it was suspended for (#2918)."""

    __tablename__ = "workspace_slot_suspensions"

    workspace_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=SLOT_SUSPENSION_REASON_ALLOWANCE
    )
    # The event that suspended it (``services.workspace_slots.SlotCause``).
    cause: Mapped[str] = mapped_column(String(32), nullable=False)
    suspended_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("reason = 'allowance'", name="ck_workspace_slot_suspension_reason"),
    )
