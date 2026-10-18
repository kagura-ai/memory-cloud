"""#2043: memories.version and the memory_revisions log.

``memories.version`` (1 on create, +1 per edit of a versioned field) backs
``update_memory(expected_version=N)``; ``memory_revisions`` holds one row per
write event (created / updated / forgotten, ``restored`` for #2044), an
``updated`` row carrying the overwritten values of the changed fields only.

Blue-green safety: ADD COLUMN with a constant ``server_default`` is
metadata-only on PG 11+ (no table rewrite, no backfill walk) and every
existing row reads ``version = 1``; old app code never references the column
or the new table. The new table and its indexes are created empty, and its
two CHECKs are on a new table, so nothing needs ``NOT VALID`` / ``VALIDATE``.

The CHECK literals are byte-identical to ``REVISION_KINDS`` /
``REVISION_WRITERS`` in ``models/memory.py`` (pinned by
``tests/test_memory_revision_constants.py``).

Revision ID: f03_2043_memory_versions
Revises: f02_2103_chunks_enabled_at
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f03_2043_memory_versions"
down_revision: str | None = "f02_2103_chunks_enabled_at"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add memories.version and create memory_revisions with its indexes."""
    op.add_column(
        "memories",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "memory_revisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memories.id", ondelete="CASCADE", name="fk_memory_revisions_memory_id"),
            nullable=False,
        ),
        sa.Column("context_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column(
            "prior",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("writer", sa.String(10), nullable=False),
        sa.Column("actor_user_id", sa.String(255), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind IN ('created', 'updated', 'forgotten', 'restored')",
            name="valid_revision_kind",
        ),
        sa.CheckConstraint("writer IN ('api', 'rollback')", name="valid_revision_writer"),
    )
    op.create_index(
        "idx_memory_revisions_memory",
        "memory_revisions",
        ["memory_id", "version", "id"],
    )
    op.create_index(
        "idx_memory_revisions_session",
        "memory_revisions",
        ["context_id", "session_id", "actor_user_id", "id"],
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop memory_revisions and memories.version (the log goes with them)."""
    op.drop_index("idx_memory_revisions_session", table_name="memory_revisions")
    op.drop_index("idx_memory_revisions_memory", table_name="memory_revisions")
    op.drop_table("memory_revisions")
    op.drop_column("memories", "version")
