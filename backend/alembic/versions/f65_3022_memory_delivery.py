"""#3022: when a memory's content was first / last delivered to a reader.

Adds ``memories.first_delivered_at`` and ``memories.last_delivered_at``
(TIMESTAMP WITHOUT TIME ZONE like the table's other timestamps, nullable, no
default). No backfill: every existing row reads NULL ("no delivery recorded"),
and no index.

Locks: each ``ADD COLUMN`` (nullable, no default) is a catalog change only —
no table rewrite and no scan — but takes a brief ``ACCESS EXCLUSIVE`` lock on
``memories``. ``alembic/env.py`` sets a ``lock_timeout`` on the migration's
transaction, so behind a long transaction on ``memories`` (a Sleep run, a bulk
import) the upgrade fails fast instead of queueing reads behind it: retry it.
No other table is locked.

Order: run this migration BEFORE starting the new code. The new code writes
the columns in the recall / reference access-statistics statement. The
previous release never names them (its statements list their columns), so it
keeps working against the migrated schema. Re-runnable (``IF NOT EXISTS`` /
``IF EXISTS``).

Downgrade drops both columns and the delivery history they hold.

Revision ID: f65_3022_memory_delivery
Revises: f64_3021_judge_switch
"""

import sqlalchemy as sa

from alembic import op

revision: str = "f65_3022_memory_delivery"
down_revision: str | None = "f64_3021_judge_switch"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "memories"
_COLUMNS = ("first_delivered_at", "last_delivered_at")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column(_TABLE, sa.Column(column, sa.DateTime(), nullable=True), if_not_exists=True)


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column(_TABLE, column, if_exists=True)
