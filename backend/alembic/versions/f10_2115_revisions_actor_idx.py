"""#2115: index ``memory_revisions.actor_user_id`` for account erasure.

``idx_memory_revisions_actor``: a plain btree on ``(actor_user_id)``. Account
erasure finds the subject's revision events by actor alone — the
session-provenance subquery, the correlated ``EXISTS`` that marks memories
whose details the subject wrote, and the UPDATE that pseudonymizes the
subject's events. Every existing ``memory_revisions`` index leads with
``memory_id`` or ``context_id``, so none of them serves ``actor_user_id = ?``.

Full, not partial: the column is NOT NULL (f03), so ``IS NOT NULL`` would be
vacuous, and a pseudonymized row is not distinguishable by any predicate.

``memory_revisions`` holds one row per write event, so the index is built
CONCURRENTLY in an autocommit block (the f05 form): writes continue during
the scan. Nothing runs before the block, so the upgrade can simply be run
again after an interrupted build; an INVALID leftover index is dropped before
the ``IF NOT EXISTS`` create. It does not run offline (``--sql``): the
INVALID-index probe needs a live connection.

Revision ID: f10_2115_revisions_actor_idx
Revises: f09_2129_cred_default_ctx
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f10_2115_revisions_actor_idx"
down_revision: str | None = "f09_2129_cred_default_ctx"
branch_labels: str | None = None
depends_on: str | None = None

_INDEX_NAME = "idx_memory_revisions_actor"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} ON memory_revisions (actor_user_id)"
)


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
    """Build the actor index without a write lock on ``memory_revisions``."""
    # CONCURRENTLY cannot run inside a transaction → the autocommit block.
    invalid = _index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the actor index, again without a write lock."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
