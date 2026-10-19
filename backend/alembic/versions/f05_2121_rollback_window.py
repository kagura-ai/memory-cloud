"""#2121: rollback_session by time window — the record key and the scan index.

- ``memory_session_rollbacks.window_since`` / ``window_until``: the window of
  a window-mode rollback (naive UTC). Nullable, no default: ADD COLUMN is
  metadata-only and every existing row (a session rollback) reads NULL.
- ``uq_memory_session_rollbacks_window``: one row per (context, principal,
  agent, since, until), partial on ``window_since IS NOT NULL``. The table is
  small and new (#2044), so a plain create inside the transaction.
- ``idx_memory_revisions_actor_at``: ``(context_id, actor_user_id, agent_id,
  at)`` for one principal's writes in a window (the rollback and
  ``changes_since(by_me=true)``). ``memory_revisions`` holds every write
  event, so it is built CONCURRENTLY in an autocommit block (the f04 / e84
  form): writes continue during the scan.

No CHECK constraint: "exactly one of session_id / the window" is enforced by
the service (an erased session row also has a NULL ``session_id``).

Restart after a failed concurrent build: the block commits everything above
it first, so the column adds and the unique index are guarded with
``if_not_exists`` and an INVALID leftover index is dropped before the
``IF NOT EXISTS`` create — the whole upgrade can simply be run again. It
does not run offline (``--sql``): the INVALID-index probe needs a live
connection.

Revision ID: f05_2121_rollback_window
Revises: f04_2044_session_provenance
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f05_2121_rollback_window"
down_revision: str | None = "f04_2044_session_provenance"
branch_labels: str | None = None
depends_on: str | None = None

_UNIQUE_NAME = "uq_memory_session_rollbacks_window"
_INDEX_NAME = "idx_memory_revisions_actor_at"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON memory_revisions (context_id, actor_user_id, agent_id, at)"
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
    """Add the window columns and their unique key, then the scan index."""
    op.add_column(
        "memory_session_rollbacks",
        sa.Column("window_since", sa.DateTime(), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "memory_session_rollbacks",
        sa.Column("window_until", sa.DateTime(), nullable=True),
        if_not_exists=True,
    )
    op.create_index(
        _UNIQUE_NAME,
        "memory_session_rollbacks",
        [
            "context_id",
            sa.text("COALESCE(principal_user_id, '')"),
            sa.text("COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "window_since",
            "window_until",
        ],
        unique=True,
        postgresql_where=sa.text("window_since IS NOT NULL"),
        if_not_exists=True,
    )
    # CONCURRENTLY (no write lock on memory_revisions) → outside the transaction.
    invalid = _index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the scan index, the unique key and the two columns."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
    op.drop_index(_UNIQUE_NAME, table_name="memory_session_rollbacks", if_exists=True)
    op.drop_column("memory_session_rollbacks", "window_until")
    op.drop_column("memory_session_rollbacks", "window_since")
