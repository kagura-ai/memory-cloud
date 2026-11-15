"""Finite authenticated manual requests and their immutable per-feed run binding."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f53_2810_sync_activity"
down_revision = "f52_2808_merge_feed_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add attribution receipts without changing existing run identities or bodies."""
    op.create_table(
        "sync_manual_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=True),
        sa.Column("token_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("browser_client_id", sa.String(255), nullable=False),
        sa.Column("source_version_hash", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("ownership_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["installation_id"], ["sync_installations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["token_id"], ["oidc_tokens.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "installation_id", "operation_id", name="uq_sync_manual_request_operation"
        ),
        sa.CheckConstraint(
            "generation > 0 AND ownership_epoch >= 0", name="ck_sync_manual_request_epoch"
        ),
        sa.CheckConstraint(
            "length(source_version_hash) = 64", name="ck_sync_manual_request_source"
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '15 minutes'",
            name="ck_sync_manual_request_expiry",
        ),
    )
    for column in ("installation_id", "requested_by", "token_id", "created_at", "expires_at"):
        op.create_index("ix_sync_manual_requests_" + column, "sync_manual_requests", [column])
    op.create_table(
        "sync_manual_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("feed_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.ForeignKeyConstraint(["run_id"], ["sync_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_id"], ["sync_manual_requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["feed_id"], ["sync_feeds.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("request_id", "feed_id", name="uq_sync_manual_run_feed"),
    )
    op.create_index("ix_sync_manual_runs_request_id", "sync_manual_runs", ["request_id"])
    op.create_index("ix_sync_manual_runs_feed_id", "sync_manual_runs", ["feed_id"])


def downgrade() -> None:
    """Drop attribution only; existing staged and committed feed records remain."""
    op.drop_table("sync_manual_runs")
    op.drop_table("sync_manual_requests")
