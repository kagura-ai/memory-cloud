"""#2044: session provenance on memories, rollback records, ``undone_by``.

- ``memories.agent_id`` / ``memories.session_id``: the last writer's verified
  agent and session. Nullable, no default: ADD COLUMN is metadata-only and
  every existing row reads NULL ("written before #2044").
- ``memory_revisions.undone_by``: the ``memory_session_rollbacks`` row that
  handled the event (nullable BIGINT, metadata-only).
- ``memory_session_rollbacks``: one row per rolled-back session target; its
  ``status`` makes a second rollback of the same session refusable. The CHECK
  literal is byte-identical to ``SESSION_ROLLBACK_STATUSES`` in
  ``models/memory.py`` (pinned by ``tests/test_session_rollback_constants.py``).
- ``idx_memories_context_session``: partial on ``session_id IS NOT NULL``. It
  matches no existing row, but building it still scans ``memories``, so it is
  built CONCURRENTLY in an autocommit block (the e84 form): writes continue
  during the scan.

Restart after a failed concurrent build: the block commits everything above
it first, so the column adds and the new table are guarded with
``if_not_exists`` and an INVALID leftover index is dropped before the
``IF NOT EXISTS`` create — the whole upgrade can simply be run again. It
does not run offline (``--sql``): the INVALID-index probe needs a live
connection.

Revision ID: f04_2044_session_provenance
Revises: f03_2043_memory_versions
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f04_2044_session_provenance"
down_revision: str | None = "f03_2043_memory_versions"
branch_labels: str | None = None
depends_on: str | None = None

_INDEX_NAME = "idx_memories_context_session"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON memories (context_id, session_id) WHERE session_id IS NOT NULL"
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
    """Add the provenance columns and the rollback table, then the index."""
    op.add_column(
        "memories",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "memories",
        sa.Column("session_id", sa.String(128), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "memory_revisions",
        sa.Column("undone_by", sa.BigInteger(), nullable=True),
        if_not_exists=True,
    )
    op.create_table(
        "memory_session_rollbacks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "context_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "contexts.id",
                ondelete="CASCADE",
                name="fk_memory_session_rollbacks_context_id",
            ),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("principal_user_id", sa.String(255), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column(
            "summary",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'partial')",
            name="valid_session_rollback_status",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "uq_memory_session_rollbacks_target",
        "memory_session_rollbacks",
        [
            "context_id",
            "session_id",
            sa.text("COALESCE(principal_user_id, '')"),
            sa.text("COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
        ],
        unique=True,
        if_not_exists=True,
    )
    # CONCURRENTLY (no write lock on memories) → outside the transaction.
    invalid = _index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the index, the rollback table and the three columns."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
    op.drop_index("uq_memory_session_rollbacks_target", table_name="memory_session_rollbacks")
    op.drop_table("memory_session_rollbacks")
    op.drop_column("memory_revisions", "undone_by")
    op.drop_column("memories", "session_id")
    op.drop_column("memories", "agent_id")
