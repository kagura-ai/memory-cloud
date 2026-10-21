"""Add the 'erasure' writer to the memory_revisions log.

Issue #2244: account erasure reverts the live ``details`` an erased member
wrote on another author's memory to the last value someone else wrote
(``services.erasure_details.restore_editor_details``). That revert is a write
event of its own — a version bump and one ``updated`` revision row — written by
the erasure, not by an API caller or a session rollback, so the
``valid_revision_writer`` vocabulary gains ``'erasure'``. The value also keeps a
re-run over a past erasure (#2243) from reverting the memory a second time.

The CHECK literal is kept byte-identical to the ordered ``REVISION_WRITERS``
tuple in ``models/memory.py`` (pinned by
``tests/test_memory_revision_constants.py``). New values are APPENDED, never
reordered.

``memory_revisions`` is written on every memory edit, so this uses the house
zero-downtime pattern (``f00_2019_llm_log_supersede``): an atomic
``DROP ..., ADD ... NOT VALID`` (ACCESS EXCLUSIVE, but no scan), then
``VALIDATE CONSTRAINT`` inside ``autocommit_block()``, which holds only SHARE
UPDATE EXCLUSIVE (#2073). The new CHECK only widens the vocabulary, so every
existing row satisfies it.

Revision ID: f12_2244_rev_writer_erasure
Revises: f11_2092_embedded_text_digest
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f12_2244_rev_writer_erasure"
down_revision: str | None = "f11_2092_embedded_text_digest"
branch_labels: str | None = None
depends_on: str | None = None


_OLD_CHECK = "writer IN ('api', 'rollback')"
_NEW_CHECK = "writer IN ('api', 'rollback', 'erasure')"


def _validate() -> None:
    """VALIDATE outside the ALTER's transaction (#2073).

    ``autocommit_block()`` commits the preceding transaction, releasing the
    NOT VALID add's ACCESS EXCLUSIVE lock, so the scan runs under SHARE UPDATE
    EXCLUSIVE only. Offline (``--sql``) it emits ``COMMIT;`` / ``BEGIN;``.
    """
    with op.get_context().autocommit_block():
        op.execute(
            sa.text("ALTER TABLE memory_revisions VALIDATE CONSTRAINT valid_revision_writer")
        )


def upgrade() -> None:
    """Widen the valid_revision_writer CHECK (zero-downtime).

    DROP + ADD are one ALTER TABLE so there is no window without a writer
    CHECK; VALIDATE runs after that ALTER commits (see the module docstring).
    """
    op.execute(
        sa.text(
            "ALTER TABLE memory_revisions "
            "DROP CONSTRAINT IF EXISTS valid_revision_writer, "
            f"ADD CONSTRAINT valid_revision_writer CHECK ({_NEW_CHECK}) NOT VALID"
        )
    )
    _validate()


def downgrade() -> None:
    """Restore the two-value CHECK; refuse while an 'erasure' row exists.

    Unlike an append-only ledger (``f00``), ``memory_revisions`` rows are
    rewritten after they are written: a later erasure strips or marks their
    ``prior``, and a NOT VALID CHECK is enforced on every UPDATE, so a leftover
    ``erasure`` row under the old CHECK would make a later account erasure
    fail. Deleting the rows would rewrite memories' history instead. The
    downgrade therefore stops before any change, naming the count; an operator
    who must go back decides what those rows become first.

    The probe runs before the ALTER, so a row written between the probe and
    the ALTER fails the ALTER's VALIDATE below rather than slipping through.
    """
    leftover = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM memory_revisions WHERE writer = 'erasure'"))
        .scalar()
    )
    if leftover:
        raise RuntimeError(
            f"#2244: {leftover} memory_revisions row(s) have writer = 'erasure' "
            "(account erasure reverted an erased member's details on another "
            "author's memory). The previous valid_revision_writer CHECK does not "
            "allow them. Stay on f12_2244_rev_writer_erasure, or decide what those "
            "rows become before downgrading."
        )
    op.execute(
        sa.text(
            "ALTER TABLE memory_revisions "
            "DROP CONSTRAINT IF EXISTS valid_revision_writer, "
            f"ADD CONSTRAINT valid_revision_writer CHECK ({_OLD_CHECK}) NOT VALID"
        )
    )
    _validate()
