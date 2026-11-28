"""#3051: the last text-edit time a context merge carries to its copy.

Adds ``memories.carried_text_edited_at`` (TIMESTAMP WITHOUT TIME ZONE like the
table's other timestamps, nullable, no default, no index). ``merge_contexts``
writes it on each copy: when an edit last wrote the text the copy holds, as
the source's revision log (and a carried time of the source, for a copy of a
copy) tells. Sleep dedup's recency rule reads it as the copy's version-1 text
time: the copy starts at version 1 and its source's edits are logged under the
source's id.

No backfill: every existing row reads NULL, a copy made before this revision
included (it is compared by its creation time, as before).

Locks: the ``ADD COLUMN`` (nullable, no default) is a catalog change only — no
table rewrite and no scan — but takes a brief ``ACCESS EXCLUSIVE`` lock on
``memories``, held until the migration's transaction commits. ``alembic/env.py``
sets a ``lock_timeout`` on the migration's transaction, so behind a long
transaction on ``memories`` (a Sleep run, a bulk import) the upgrade fails fast
instead of queueing reads behind it: retry it. No other table is locked, no
constraint, index or foreign key is added.

Order: run this migration BEFORE starting the new code. The column is mapped,
so every statement of the new code that selects ``memories`` names it. The
previous release never names it (its statements list their columns, and its
merge INSERT leaves it NULL), so it keeps working against the migrated schema.
Re-runnable (``IF NOT EXISTS`` / ``IF EXISTS``).

Downgrade drops the column and the carried times it holds.

Revision ID: f68_3051_carried_text_edit
Revises: f67_3035_public_visitors
"""

import sqlalchemy as sa

from alembic import op

revision: str = "f68_3051_carried_text_edit"
down_revision: str | None = "f67_3035_public_visitors"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("carried_text_edited_at", sa.DateTime(), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("memories", "carried_text_edited_at", if_exists=True)
