"""Add 'explore' to memory_access_events.valid_mae_operation CHECK.

Issue #1401: explore was outside the MAE operation vocabulary, so no
memory_access_events row could ever be written for it (allows and denies
were equally invisible to the append-only audit trail). #1400 additionally
routes explore's context resolution through the read-path helper with
operation="explore", so an enforce-mode binding deny now emits an audit
row — which requires 'explore' to be a permitted operation value.

The CHECK literal is kept byte-identical to the ordered ``MAE_OPERATIONS``
tuple in ``models/memory_access_event.py`` (the house drift-pin convention,
pinned by ``tests/test_memory_access_event_constants.py``). New values are
APPENDED, never reordered.

``memory_access_events`` is written SYNCHRONOUSLY on the request critical path
(``memory_access_event_writer.record_memory_access_event`` does ``await
db.commit()`` inline, awaited by recall/reference/remember/explore/permission
checks). A plain ``ADD CONSTRAINT ... CHECK`` (as emitted by
``op.create_check_constraint``) validates every existing row while holding
ACCESS EXCLUSIVE, stalling those audit writes for the scan's duration. So this
uses the house zero-downtime pattern (``d05_523``/``d07_495``): an atomic
``DROP ... , ADD ... NOT VALID`` followed by a separate ``VALIDATE CONSTRAINT``
(which runs under SHARE UPDATE EXCLUSIVE — reads/writes continue). Because each
new CHECK only WIDENS the vocabulary, every existing row already satisfies it
and VALIDATE is an effectively free scan.

Revision ID: e74_1401_mae_explore
Revises: e73_1377_locale_backfill
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e74_1401_mae_explore"
down_revision = "e73_1377_locale_backfill"
branch_labels = None
depends_on = None


_OLD_CHECK = (
    "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
    "'load_pinned', 'bootstrap', 'feedback')"
)
_NEW_CHECK = (
    "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
    "'load_pinned', 'bootstrap', 'feedback', 'explore')"
)


def upgrade() -> None:
    """Add 'explore' to the valid_mae_operation CHECK (zero-downtime).

    DROP + ADD are combined in one ALTER TABLE so the catalog mutation is
    atomic (no window where the table has no operation CHECK); VALIDATE is a
    separate statement per Postgres semantics.
    """
    op.execute(
        sa.text(
            "ALTER TABLE memory_access_events "
            "DROP CONSTRAINT IF EXISTS valid_mae_operation, "
            f"ADD CONSTRAINT valid_mae_operation CHECK ({_NEW_CHECK}) NOT VALID"
        )
    )
    op.execute(sa.text("ALTER TABLE memory_access_events VALIDATE CONSTRAINT valid_mae_operation"))


def downgrade() -> None:
    """Remove 'explore' from the valid_mae_operation CHECK constraint.

    Note: fails at VALIDATE if any rows with operation='explore' exist. Delete
    them first:
        DELETE FROM memory_access_events WHERE operation = 'explore';
    """
    op.execute(
        sa.text(
            "ALTER TABLE memory_access_events "
            "DROP CONSTRAINT IF EXISTS valid_mae_operation, "
            f"ADD CONSTRAINT valid_mae_operation CHECK ({_OLD_CHECK}) NOT VALID"
        )
    )
    op.execute(sa.text("ALTER TABLE memory_access_events VALIDATE CONSTRAINT valid_mae_operation"))
