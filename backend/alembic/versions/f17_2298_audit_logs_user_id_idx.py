"""#2298: index ``audit_logs.user_id``.

``ix_audit_logs_user_id``: a plain btree on ``(user_id)``, named by the
``ix_%(table)s_%(column_0_name)s`` convention of the baseline's
``ix_audit_logs_user_email``. ``user_id`` was the one identity column of
``audit_logs`` without an index, yet it is read by account erasure inside
its Postgres transaction (the ordered ``FOR UPDATE`` and the UPDATE of the
subject's own rows, ``WHERE user_id = :sub``), by ``reprocess_erasures``
(the subject's rows by account id or pseudonym) and by the ops lookup of a
member's audit trail. The table grows with users and usage (terms
acceptance, feedback, sign-in events), not only with admin actions.

Full, not partial: the column is NOT NULL, and a pseudonymized row is not
distinguishable by any predicate.

Built CONCURRENTLY in an autocommit block (the f10 form): writes continue
during the scan. Only the read-only INVALID probe runs before the block, so
the upgrade can simply be run again after an interrupted build; an INVALID
leftover index is dropped before the ``IF NOT EXISTS`` create. It does not
run offline (``--sql``): the INVALID-index probe needs a live connection.

Revision ID: f17_2298_audit_logs_user_id_idx
Revises: f16_2330_observed_at
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f17_2298_audit_logs_user_id_idx"
down_revision: str | None = "f16_2330_observed_at"
branch_labels: str | None = None
depends_on: str | None = None

_INDEX_NAME = "ix_audit_logs_user_id"
# Literal DDL (identifiers cannot be bound parameters).
_INDEX_DDL = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_logs_user_id ON audit_logs (user_id)"
_DROP_DDL = "DROP INDEX CONCURRENTLY IF EXISTS ix_audit_logs_user_id"


def _index_is_invalid(name: str) -> bool:
    """Return True if ``name`` exists in ``pg_index`` in an INVALID state (e84)."""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND i.indisvalid IS FALSE"
            ),
            {"name": name},
        )
        .first()
    )
    return row is not None


def upgrade() -> None:
    """Build the user_id index without a write lock on ``audit_logs``."""
    # CONCURRENTLY cannot run inside a transaction → the autocommit block.
    invalid = _index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(_DROP_DDL))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the user_id index, again without a write lock."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(_DROP_DDL))
