"""#3053: per-day counts of public usage events, and the days that are complete.

Creates ``public_mcp_usage_day_counts``: the ``tool_call`` events of one
endpoint and UTC day by workspace, tool, outcome, route and caller
(primary key in that order, every column NOT NULL, ``calls`` BIGINT with
the CHECK ``pmudc_calls_positive``) and the purge index ``ix_pmudc_day``;
and ``public_mcp_usage_closed_days`` (``day`` primary key, ``closed_at``):
the days whose counts equal their events. No foreign key.

Counts nothing: the hourly job closes the finished days, existing events
included, each day in a transaction of its own after the new code runs
(``services/public_mcp_usage_rollup.py``); until a day is closed the owner
statistics count it from the events, as before.

Locks: ``CREATE TABLE`` and ``CREATE INDEX`` on the two new, empty tables
take ``ACCESS EXCLUSIVE`` on those tables only. No lock is taken on
``public_mcp_usage_events``, ``users``, ``workspaces``, ``contexts`` or any
other table.

Order: run this migration BEFORE starting the new code: the new usage
writer adds every batch's calls to ``public_mcp_usage_day_counts`` in the
batch's transaction. The previous release never names the new tables and
keeps working against the migrated schema (the days it writes events on are
counted again when they are closed). Re-runnable (``IF NOT EXISTS``).

Downgrade drops both tables.

Revision ID: f69_3053_usage_rollup
Revises: f68_3051_carried_text_edit
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f69_3053_usage_rollup"
down_revision: str | None = "f68_3051_carried_text_edit"
branch_labels: str | None = None
depends_on: str | None = None

_COUNTS = "public_mcp_usage_day_counts"
_CLOSED = "public_mcp_usage_closed_days"


def upgrade() -> None:
    op.create_table(
        _COUNTS,
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("route", sa.String(8), nullable=False),
        sa.Column("caller", sa.String(16), nullable=False),
        sa.Column("calls", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "endpoint_id", "day", "workspace_id", "tool", "outcome", "route", "caller"
        ),
        sa.CheckConstraint("calls > 0", name="pmudc_calls_positive"),
        if_not_exists=True,
    )
    op.create_index("ix_pmudc_day", _COUNTS, ["day"], if_not_exists=True)
    op.create_table(
        _CLOSED,
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("day"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table(_CLOSED, if_exists=True)
    op.drop_index("ix_pmudc_day", table_name=_COUNTS, if_exists=True)
    op.drop_table(_COUNTS, if_exists=True)
