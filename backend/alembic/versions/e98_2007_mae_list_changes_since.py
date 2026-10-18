"""Add the list / changes_since / recall_upcoming / recall_nearby MAE operations.

Issue #2007: the #1852 read lanes ``list`` and ``changes_since`` thread
``operation="list"`` / ``"changes_since"`` into the read pre-gate
(``_resolve_context_for_read``), but neither value was in the MAE operation
vocabulary. On an agent-binding deny the audit writer's enum check raised
``ValueError`` and the handler answered ``validation_error`` instead of the
uniform ``context_not_found`` every other read tool returns (shadow mode
failed the same way instead of proceeding with a ``would_deny`` row).
``recall_upcoming`` and ``recall_nearby`` resolved their context with no
operation at all, so an agent-binding deny there returned the uniform 404 but
wrote no audit row; they now thread their own values too.

The CHECK literal is kept byte-identical to the ordered ``MAE_OPERATIONS``
tuple in ``models/memory_access_event.py`` (pinned by
``tests/test_memory_access_event_constants.py``). New values are APPENDED,
never reordered.

``memory_access_events`` is written synchronously on the request path, so this
uses the house zero-downtime pattern (``e74_1401`` / ``e84_1619``): an atomic
``DROP ..., ADD ... NOT VALID`` followed by ``VALIDATE CONSTRAINT`` inside
``autocommit_block()``, so the NOT VALID add commits and releases its ACCESS
EXCLUSIVE lock before the scan, which holds only SHARE UPDATE EXCLUSIVE (reads
and writes continue; #2073). The new CHECK only widens the vocabulary, so
every existing row satisfies it.

Revision ID: e98_2007_mae_list_changes_since
Revises: e97_1975_memories_per_day_addon
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e98_2007_mae_list_changes_since"
down_revision: str | None = "e97_1975_memories_per_day_addon"
branch_labels: str | None = None
depends_on: str | None = None


_OLD_CHECK = (
    "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
    "'load_pinned', 'bootstrap', 'feedback', 'explore', 'load_guardrails')"
)
_NEW_CHECK = (
    "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
    "'load_pinned', 'bootstrap', 'feedback', 'explore', 'load_guardrails', 'list', "
    "'changes_since', 'recall_upcoming', 'recall_nearby')"
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
    """Widen the valid_mae_operation CHECK (zero-downtime).

    DROP + ADD are one ALTER TABLE so there is no window without an operation
    CHECK; VALIDATE runs after that ALTER commits (see the module docstring).
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
    """Restore the previous CHECK, keeping the audit rows.

    ``memory_access_events`` is append-only, so rows with any of the four new
    operations are never deleted here (the e84_1619 form). The
    previous CHECK is restored ``NOT VALID`` — enforced on every new INSERT /
    UPDATE, and a downgraded server never writes these operations — and is
    VALIDATEd only when no such row exists. With rows present the constraint
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
        .execute(
            sa.text(
                "SELECT 1 FROM memory_access_events "
                "WHERE operation IN ('list', 'changes_since', 'recall_upcoming', "
                "'recall_nearby') LIMIT 1"
            )
        )
        .first()
    )
    if leftover is None:
        _validate()
