"""Durable embedding erasure policy and retryable UUID cleanup scopes (#2391)."""

import sqlalchemy as sa

from alembic import op

revision: str = "f19_2391_embedding_erasure"
down_revision: str | None = "f18_2395_file_cleanup"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_suppressed_at timestamptz")
    op.execute(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_erasure_restored_at timestamptz"
    )
    op.create_table(
        "vector_erasure_jobs",
        sa.Column("id", sa.UUID(), nullable=False, primary_key=True),
        sa.Column(
            "request_id",
            sa.UUID(),
            sa.ForeignKey("erasure_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("memory_id", sa.UUID(), nullable=False),
        sa.Column("point_id", sa.UUID(), nullable=False),
        sa.Column("subject_pseudonym", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "captured_before_erasure", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("request_id", "memory_id", "point_id", name="uq_vector_erasure_scope"),
        if_not_exists=True,
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vector_erasure_pending ON vector_erasure_jobs (completed_at, id)"
    )
    # Release the additive DDL locks before touching live rows. Every batch
    # is its own transaction, with timeout local to the same SQL statement
    # (safe with PgBouncer transaction pooling). Retry after interruption is
    # idempotent: the DDL is IF NOT EXISTS and completed batches no longer match.
    marker = "erased_author: the author's account was erased; its vector points are deleted by design (admin retry-failed re-embeds under the pseudonym)"
    with op.get_context().autocommit_block():
        statement = sa.text(
            "WITH timeout AS MATERIALIZED (SELECT set_config('lock_timeout', '5s', true)), locked AS MATERIALIZED (SELECT memories.id FROM memories CROSS JOIN timeout WHERE embedding_suppressed_at IS NULL AND embedding_error = :marker ORDER BY memories.id LIMIT 500 FOR NO KEY UPDATE OF memories) UPDATE memories SET embedding_suppressed_at = COALESCE(embedding_attempted_at, CURRENT_TIMESTAMP) WHERE id IN (SELECT id FROM locked)"
        )
        while op.get_bind().execute(statement, {"marker": marker}).rowcount:
            pass


def downgrade() -> None:
    op.drop_table("vector_erasure_jobs")
    op.drop_column("memories", "embedding_suppressed_at")
    op.drop_column("memories", "embedding_erasure_restored_at")
