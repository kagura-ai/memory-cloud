"""Source-backed related indexes prepared during Sleep."""

import sqlalchemy as sa

from alembic import op

revision = "f32_2728_sleep_related_index"
down_revision = "f31_2724_event_dates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sleep_related_indexes",
        sa.Column(
            "edge_id",
            sa.BigInteger(),
            sa.ForeignKey("neural_memory_edges.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "source_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "target_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "context_id",
            sa.Uuid(),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("report_id", sa.Uuid(), sa.ForeignKey("sleep_reports.id", ondelete="SET NULL")),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
    )
    for suffix, columns in (
        ("source", ["source_id", "context_id"]),
        ("target", ["target_id", "context_id"]),
        ("context", ["context_id"]),
        ("workspace", ["workspace_id"]),
        ("report", ["report_id"]),
    ):
        op.create_index("ix_sleep_related_" + suffix, "sleep_related_indexes", columns)


def downgrade() -> None:
    op.drop_table("sleep_related_indexes")
