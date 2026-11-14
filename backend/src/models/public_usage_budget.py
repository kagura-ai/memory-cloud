"""The workspace-wide seven-day budget of anonymous public calls (#2805).

One row per workspace that has made a counted public call: the running
period's start, its count and its sequence number. Written only by
``services.public_usage_budget`` (one statement per charge, on a session of
its own); the contract is ``docs/public-usage-budget.md``. No endpoint,
address, client, plan or limit is stored: the limit is a deployment setting
read at each charge.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.api_base import TZAwareBaseModel

PublicUsageBudgetState = Literal["disabled", "idle", "active", "exhausted"]


class PublicUsageBudget(Base):
    """The running budget period of one workspace.

    Attributes:
        workspace_id: The workspace (the only key; cascades with it).
        period_started_at: Database time of the period's first counted call (naive UTC).
        calls_used: Counted calls in that period.
        period_seq: The period's number: 1 for the first, +1 at each rollover.
        updated_at: Database time of the last write (naive UTC).
    """

    __tablename__ = "public_usage_budgets"
    __table_args__ = (
        CheckConstraint("calls_used >= 0", name="ck_public_usage_budgets_calls_used"),
        CheckConstraint("period_seq >= 1", name="ck_public_usage_budgets_period_seq"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    period_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    calls_used: Mapped[int] = mapped_column(Integer, nullable=False)
    period_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PublicUsageBudgetStatus(TZAwareBaseModel):
    """The owner's view of the workspace's public usage budget.

    Workspace-wide: every public endpoint of the workspace shares it.
    """

    state: PublicUsageBudgetState = Field(
        ...,
        description=(
            "disabled: the operator switch is off; idle: no period runs (the next "
            "counted call starts one); active: a period runs with calls left; "
            "exhausted: the period's calls are used"
        ),
    )
    limit: int | None = Field(None, description="Counted calls per period (null when disabled)")
    period_days: int = Field(7, description="Length of a period in days")
    used: int | None = Field(None, description="Counted calls in the running period")
    remaining: int | None = Field(None, description="Calls left in the running period")
    period_started_at: datetime | None = Field(
        None, description="Time of the running period's first counted call"
    )
    resets_at: datetime | None = Field(
        None, description="End of the running period (period_started_at + 7 days)"
    )
    period_seq: int | None = Field(None, description="Number of the running period")
    as_of: datetime = Field(..., description="Database time of this read")
