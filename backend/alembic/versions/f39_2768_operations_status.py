"""#2768: ``scheduled_job_status`` and ``operations_llm_lanes``.

Two small tables the scheduler leader writes and any API process reads for
the operations status route. Aggregates only: code- or deployment-defined
names, times, integers and exception class names; no user, workspace or
context identifier.

- ``scheduled_job_status``: one row per scheduled job id, updated in place
  (no history). CHECKs on the trigger kind, the outcome, a positive expected
  interval, non-negative failures and duration, and a 2 KB cap on the
  serialized ``last_counts``.
- ``operations_llm_lanes``: one row per platform LLM lane (CHECK on the six
  lane names), its provider / model / credential presence as the leader sees
  them, and the latest opt-in probe result.

Plain ``create_table``: new, empty tables, no foreign keys, nothing to
backfill, no secondary index (about 25 and at most 6 rows).

Downgrade drops both tables (the recorded state is lost; the next leader
registration recreates the job rows after an upgrade).

Revision ID: f39_2768_operations_status
Revises: f38_2761_announcement_consents

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f39_2768_operations_status"
down_revision: str | None = "f38_2761_announcement_consents"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``scheduled_job_status`` and ``operations_llm_lanes``."""
    op.create_table(
        "scheduled_job_status",
        sa.Column("job_id", sa.String(length=100), nullable=False),
        sa.Column("job_name", sa.String(length=200), nullable=False),
        sa.Column("trigger_kind", sa.String(length=10), nullable=False),
        sa.Column("expected_interval_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "first_registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registered_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(length=10), nullable=True),
        sa.Column("last_outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error_class", sa.String(length=100), nullable=True),
        sa.Column("last_duration_ms", sa.Integer(), nullable=True),
        sa.Column("last_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("job_id"),
        sa.CheckConstraint(
            "trigger_kind IN ('interval', 'cron', 'other')",
            name="valid_scheduled_job_trigger_kind",
        ),
        sa.CheckConstraint(
            "last_outcome IS NULL OR last_outcome IN ('ok', 'failed', 'missed')",
            name="valid_scheduled_job_outcome",
        ),
        sa.CheckConstraint(
            "expected_interval_seconds IS NULL OR expected_interval_seconds > 0",
            name="valid_scheduled_job_interval",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="valid_scheduled_job_failures",
        ),
        sa.CheckConstraint(
            "last_duration_ms IS NULL OR last_duration_ms >= 0",
            name="valid_scheduled_job_duration",
        ),
        sa.CheckConstraint(
            "last_counts IS NULL OR octet_length(last_counts::text) <= 2048",
            name="valid_scheduled_job_counts_size",
        ),
    )
    op.create_table(
        "operations_llm_lanes",
        sa.Column("lane", sa.String(length=30), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("credential_present", sa.Boolean(), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_outcome", sa.String(length=10), nullable=True),
        sa.Column("last_probe_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_probe_error_class", sa.String(length=100), nullable=True),
        sa.Column(
            "probe_consecutive_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_probe_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("lane"),
        sa.CheckConstraint(
            "lane IN ('embedding', 'sleep_judge', 'supersede_verify', 'recall_selector', "
            "'analysis', 'rerank')",
            name="valid_operations_llm_lane",
        ),
        sa.CheckConstraint(
            "last_probe_outcome IS NULL OR last_probe_outcome IN ('ok', 'failed')",
            name="valid_operations_llm_lane_probe_outcome",
        ),
        sa.CheckConstraint(
            "probe_consecutive_failures >= 0",
            name="valid_operations_llm_lane_probe_failures",
        ),
        sa.CheckConstraint(
            "last_probe_latency_ms IS NULL OR last_probe_latency_ms >= 0",
            name="valid_operations_llm_lane_probe_latency",
        ),
    )


def downgrade() -> None:
    """Drop both tables."""
    op.drop_table("operations_llm_lanes")
    op.drop_table("scheduled_job_status")
