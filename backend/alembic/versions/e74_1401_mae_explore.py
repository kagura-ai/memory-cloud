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

Revision ID: e74_1401_mae_explore
Revises: e73_1377_locale_backfill
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e74_1401_mae_explore"
down_revision = "e73_1377_locale_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add 'explore' to the valid_mae_operation CHECK constraint."""
    op.drop_constraint("valid_mae_operation", "memory_access_events", type_="check")
    op.create_check_constraint(
        "valid_mae_operation",
        "memory_access_events",
        "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
        "'load_pinned', 'bootstrap', 'feedback', 'explore')",
    )


def downgrade() -> None:
    """Remove 'explore' from the valid_mae_operation CHECK constraint.

    Note: fails if any rows with operation='explore' exist. Delete them first:
        DELETE FROM memory_access_events WHERE operation = 'explore';
    """
    op.drop_constraint("valid_mae_operation", "memory_access_events", type_="check")
    op.create_check_constraint(
        "valid_mae_operation",
        "memory_access_events",
        "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
        "'load_pinned', 'bootstrap', 'feedback')",
    )
