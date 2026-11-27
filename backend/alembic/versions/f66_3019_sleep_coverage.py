"""#3019: Sleep coverage positions and the importance re-evaluation marker.

Adds ``sleep_reports.coverage_state`` (JSON, nullable, no default): the
positions of the Sleep phases that work through a context in a stable order
(dedup and the consolidation judge), written by the run that advanced them in
that run's transaction. The current state of a partition is that of its
latest report that has one.

Adds ``memories.importance_reevaluated_at`` (TIMESTAMP WITHOUT TIME ZONE like
the table's other timestamps, nullable, no default, no index): when importance
re-evaluation last judged the row. Re-evaluation no longer writes
``updated_at``; its seven-day cadence reads this column.

No backfill: every existing row reads NULL (no position: the first run starts
from the beginning; no marker: a row's cadence follows its ``updated_at`` /
``created_at`` as before).

Locks: each ``ADD COLUMN`` (nullable, no default) is a catalog change only —
no table rewrite and no scan — but takes a brief ``ACCESS EXCLUSIVE`` lock,
first on ``sleep_reports``, then on ``memories``. ``alembic/env.py`` sets a
``lock_timeout`` on the migration's transaction, so behind a long transaction
on either table (a Sleep run holds both) the upgrade fails fast instead of
queueing reads behind it: retry it. No other table is locked, no foreign key
is added.

Order: run this migration BEFORE starting the new code. Both columns are
mapped, so every statement of the new code that selects these tables names
them. The previous release never names them (its statements list their
columns), so it keeps working against the migrated schema. Re-runnable
(``IF NOT EXISTS`` / ``IF EXISTS``).

Downgrade drops both columns, the positions and the markers they hold.

Revision ID: f66_3019_sleep_coverage
Revises: f65_3022_memory_delivery
"""

import sqlalchemy as sa

from alembic import op

revision: str = "f66_3019_sleep_coverage"
down_revision: str | None = "f65_3022_memory_delivery"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sleep_reports",
        sa.Column("coverage_state", sa.JSON(), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "memories",
        sa.Column("importance_reevaluated_at", sa.DateTime(), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("memories", "importance_reevaluated_at", if_exists=True)
    op.drop_column("sleep_reports", "coverage_state", if_exists=True)
