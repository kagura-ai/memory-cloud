"""Queue for the vector point deletes forget defers to a drain job (#2476).

Additive: a new table nothing references, so no lock on an existing table.
Apply before starting code that forgets: forget inserts into it. Downgrade
drops the table and any pending retries; the orphan vector sweep still
removes those points.
"""

import sqlalchemy as sa

from alembic import op

revision: str = "f21_2476_deferred_point_deletes"
down_revision: str | None = "f20_2399_oauth_token_storage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # No foreign key to memories: the 30-day purge and erasure hard-delete
    # rows, and a cascade would drop the retry with them.
    op.create_table(
        "deferred_point_deletes",
        sa.Column("id", sa.UUID(), nullable=False, primary_key=True),
        sa.Column("memory_id", sa.UUID(), nullable=False),
        sa.Column("point_id", sa.UUID(), nullable=False),
        sa.Column("collection", sa.String(255), nullable=False),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("memory_id", "point_id", "collection", name="uq_deferred_point_delete"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_deferred_point_deletes_queued",
        "deferred_point_deletes",
        ["queued_at", "id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("deferred_point_deletes")
