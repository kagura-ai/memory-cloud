"""Atomic staged product/FAQ snapshots and durable indexing handoff (#2807)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "f48_2807_sync_feed_runs"
down_revision = "f47_2819_merge_sync_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add new tables only; never rewrite existing targets, data or credentials."""
    op.create_table(
        "sync_feeds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "installation_id",
            sa.Uuid(),
            sa.ForeignKey("sync_installations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('product', 'faq')", name="ck_sync_feed_kind"),
        sa.CheckConstraint("revision >= 0", name="ck_sync_feed_revision"),
        sa.CheckConstraint("config_version > 0", name="ck_sync_feed_config_version"),
        sa.CheckConstraint("length(config_hash) = 64", name="ck_sync_feed_config_hash"),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "feed_id", sa.Uuid(), sa.ForeignKey("sync_feeds.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("manifest", pg.JSONB(), nullable=False),
        sa.Column("generations", pg.JSONB(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("diff", pg.JSONB()),
        sa.Column("diff_hash", sa.String(64)),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("prepared_until", sa.DateTime()),
        sa.Column("approval_id", sa.Uuid()),
        sa.Column(
            "approved_by", sa.String(255), sa.ForeignKey("users.user_id", ondelete="SET NULL")
        ),
        sa.Column("approval_epoch", sa.Integer()),
        sa.Column("commit_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("committed_at", sa.DateTime()),
        sa.UniqueConstraint("feed_id", "operation_id", name="uq_sync_run_operation"),
        sa.CheckConstraint(
            "state IN ('uploading', 'prepared', 'committed', 'aborted', 'expired')",
            name="ck_sync_run_state",
        ),
        sa.CheckConstraint("base_revision >= 0", name="ck_sync_run_revision"),
        sa.CheckConstraint("expires_at > created_at", name="ck_sync_run_expiry"),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_sync_run_request_hash"),
    )
    op.create_table(
        "sync_chunks",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("chunk_index", sa.Integer(), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("records", pg.JSONB()),
        sa.CheckConstraint("chunk_index >= 0 AND chunk_index < 1000", name="ck_sync_chunk_index"),
        sa.CheckConstraint("byte_count > 0 AND byte_count <= 524288", name="ck_sync_chunk_bytes"),
        sa.CheckConstraint("length(digest) = 64", name="ck_sync_chunk_digest"),
        sa.CheckConstraint(
            "record_count > 0 AND record_count <= 100", name="ck_sync_chunk_records"
        ),
    )
    op.create_table(
        "sync_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "feed_id", sa.Uuid(), sa.ForeignKey("sync_feeds.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "resource_pk",
            sa.Uuid(),
            sa.ForeignKey("resources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("doc_id", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", pg.JSONB()),
        sa.Column(
            "last_event_id",
            sa.BigInteger(),
            sa.ForeignKey("resource_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("feed_id", "external_id", name="uq_sync_record_external"),
        sa.UniqueConstraint("resource_pk", "doc_id", name="uq_sync_record_document"),
        sa.CheckConstraint("version > 0", name="ck_sync_record_version"),
    )
    op.create_table(
        "sync_index_outbox",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "resource_pk",
            sa.Uuid(),
            sa.ForeignKey("resources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "context_id",
            sa.Uuid(),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("last_event_id", sa.BigInteger(), nullable=False),
        sa.Column("event_ids", pg.JSONB(), nullable=False),
        sa.Column("delivered_at", sa.DateTime()),
    )
    for table, columns in {
        "sync_feeds": ("installation_id",),
        "sync_runs": ("feed_id", "approved_by"),
        "sync_records": ("feed_id", "resource_pk", "last_event_id"),
        "sync_index_outbox": ("resource_pk", "context_id"),
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    """Remove feed control/projection tables, retaining all Resource/Memory data.

    Disable sync_runs_enabled and back up canonical/staging tables before a
    deliberate rollback. A downgrade cannot undo already committed content.
    """
    for table in ("sync_index_outbox", "sync_records", "sync_chunks", "sync_runs", "sync_feeds"):
        op.drop_table(table)
