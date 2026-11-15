"""Persist managed-data stop barriers and bounded, human-approved resume snapshots (#2808)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "f51_2808_managed_sync"
down_revision = "f50_2807_merge_sync_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """New operational/raw tables only; enrollment is an explicit rollout action."""
    op.create_table(
        "sync_managed_targets",
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("resource_pk", sa.UUID(), nullable=False),
        sa.Column("context_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("event_floor", sa.BigInteger(), nullable=False),
        sa.Column("stop_event_id", sa.BigInteger(), nullable=False),
        sa.Column("drain_generation", sa.Integer(), nullable=True),
        sa.Column("stop_reason", sa.String(length=40), nullable=True),
        sa.Column("manual_sleep_mode", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("epoch > 0 AND generation > 0", name="ck_sync_managed_generation"),
        sa.CheckConstraint("mode IN ('feed', 'stopping', 'manual')", name="ck_sync_managed_mode"),
        sa.CheckConstraint(
            "event_floor >= 0 AND stop_event_id >= 0", name="ck_sync_managed_offsets"
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["sync_installations.id"],
            name="fk_sync_managed_targets_installation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("installation_id"),
        sa.UniqueConstraint("context_id", name="sync_managed_targets_context_id_key"),
        sa.UniqueConstraint("resource_pk", name="sync_managed_targets_resource_pk_key"),
    )
    op.create_table(
        "sync_resume_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("feed_id", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("generations", pg.JSONB(), nullable=False),
        sa.Column("manifest", pg.JSONB(), nullable=False),
        sa.Column("records", pg.JSONB(), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "byte_count > 0 AND byte_count <= 20971520", name="ck_sync_candidate_bytes"
        ),
        sa.CheckConstraint(
            "record_count >= 0 AND record_count <= 10000", name="ck_sync_candidate_count"
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '24 hours'",
            name="ck_sync_candidate_expiry",
        ),
        sa.CheckConstraint("length(snapshot_hash) = 64", name="ck_sync_candidate_hash"),
        sa.ForeignKeyConstraint(
            ["feed_id"],
            ["sync_feeds.id"],
            name="fk_sync_resume_candidates_feed_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["id"], ["sync_runs.id"], name="fk_sync_resume_candidates_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["sync_installations.id"],
            name="fk_sync_resume_candidates_installation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sync_resume_candidates_feed_id", "sync_resume_candidates", ["feed_id"], unique=False
    )
    op.create_index(
        "ix_sync_resume_candidates_installation_id",
        "sync_resume_candidates",
        ["installation_id"],
        unique=False,
    )
    op.create_index(
        "uq_sync_candidate_active_feed",
        "sync_resume_candidates",
        ["feed_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "sync_resume_previews",
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("ownership_epoch", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("manual_hash", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("diff_hash", sa.String(length=64), nullable=False),
        sa.Column("totals", pg.JSONB(), nullable=False),
        sa.Column("diff", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '15 minutes'",
            name="ck_sync_preview_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.user_id"],
            name="fk_sync_resume_previews_approved_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["sync_installations.id"],
            name="fk_sync_resume_previews_installation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("installation_id"),
        sa.UniqueConstraint("id", name="sync_resume_previews_id_key"),
    )
    op.create_table(
        "sync_mode_operations",
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.String(length=255), nullable=True),
        sa.Column("result", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "action IN ('stop', 'finalize', 'resume', 'reconnect')",
            name="ck_sync_mode_operation_action",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.user_id"],
            name="fk_sync_mode_operations_actor_user_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["sync_installations.id"],
            name="fk_sync_mode_operations_installation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("installation_id", "operation_id"),
    )
    op.create_table(
        "sync_manual_retirements",
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("memory_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "generation > 0 AND memory_version > 0 AND length(content_hash) = 64",
            name="ck_sync_retirement_proof",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.user_id"],
            name="fk_sync_manual_retirements_approved_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["sync_installations.id"],
            name="fk_sync_manual_retirements_installation_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memories.id"],
            name="fk_sync_manual_retirements_memory_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("installation_id", "memory_id"),
    )
    op.create_index("ix_sync_resume_previews_approved_by", "sync_resume_previews", ["approved_by"])
    op.create_index(
        "ix_sync_mode_operations_actor_user_id", "sync_mode_operations", ["actor_user_id"]
    )
    op.create_index(
        "ix_sync_manual_retirements_memory_id", "sync_manual_retirements", ["memory_id"]
    )
    op.create_index(
        "ix_sync_manual_retirements_approved_by", "sync_manual_retirements", ["approved_by"]
    )


def downgrade() -> None:
    """Drop the new control/snapshot tables; committed feed content is retained."""
    op.drop_table("sync_manual_retirements")
    op.drop_table("sync_mode_operations")
    op.drop_table("sync_resume_previews")
    op.drop_table("sync_resume_candidates")
    op.drop_table("sync_managed_targets")
