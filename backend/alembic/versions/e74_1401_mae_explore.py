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
uses the house zero-downtime pattern: an atomic ``DROP ... , ADD ... NOT
VALID`` followed by ``VALIDATE CONSTRAINT`` inside ``autocommit_block()``.
``alembic/env.py`` runs an upgrade in one transaction, so a VALIDATE in that
transaction would scan under the ALTER's ACCESS EXCLUSIVE lock until commit
(#2073); the block commits the NOT VALID add first, and VALIDATE then holds
only SHARE UPDATE EXCLUSIVE (reads/writes continue). Because each new CHECK
only WIDENS the vocabulary, every existing row already satisfies it.

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


def _validate() -> None:
    """VALIDATE outside the ALTER's transaction (#2073).

    ``autocommit_block()`` commits the preceding transaction, releasing the
    NOT VALID add's ACCESS EXCLUSIVE lock, so the scan runs under SHARE UPDATE
    EXCLUSIVE only. Offline (``--sql``) it emits ``COMMIT;`` / ``BEGIN;`` —
    for the upgrade only: the downgrade's leftover probe reads the table, so
    it needs a live connection.
    """
    with op.get_context().autocommit_block():
        op.execute(
            sa.text("ALTER TABLE memory_access_events VALIDATE CONSTRAINT valid_mae_operation")
        )


def upgrade() -> None:
    """Add 'explore' to the valid_mae_operation CHECK (zero-downtime).

    DROP + ADD are combined in one ALTER TABLE so the catalog mutation is
    atomic (no window where the table has no operation CHECK); VALIDATE runs
    after that ALTER commits (see the module docstring).
    """
    op.execute(
        sa.text(
            "ALTER TABLE memory_access_events "
            "DROP CONSTRAINT IF EXISTS valid_mae_operation, "
            f"ADD CONSTRAINT valid_mae_operation CHECK ({_NEW_CHECK}) NOT VALID"
        )
    )
    _validate()


def downgrade() -> None:
    """Remove 'explore' from the valid_mae_operation CHECK, keeping the audit rows.

    The previous CHECK is restored ``NOT VALID`` and VALIDATEd only when no
    ``operation='explore'`` row exists (the e84_1619 form). The VALIDATE runs
    after the ALTER commits (#2073), so an unguarded VALIDATE failing on such a
    row would leave the old CHECK committed under an e74 ``alembic_version``.
    The check runs while the ALTER's lock is held and, once that commits, the
    NOT VALID CHECK rejects new such rows. With rows present the constraint
    stays ``convalidated = false``; re-running the upgrade validates it again.
    """
    op.execute(
        sa.text(
            "ALTER TABLE memory_access_events "
            "DROP CONSTRAINT IF EXISTS valid_mae_operation, "
            f"ADD CONSTRAINT valid_mae_operation CHECK ({_OLD_CHECK}) NOT VALID"
        )
    )
    leftover = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM memory_access_events WHERE operation = 'explore' LIMIT 1"))
        .first()
    )
    if leftover is None:
        _validate()
