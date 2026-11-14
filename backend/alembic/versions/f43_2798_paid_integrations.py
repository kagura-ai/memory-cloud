"""Finite, ordered workspace paid integration rights (#2798).

Revision ID: f43_2798_paid_integrations
Revises: f42_2762_mcp_notices
"""

import sqlalchemy as sa

from alembic import op

revision: str = "f43_2798_paid_integrations"
down_revision: str | None = "f42_2762_mcp_notices"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create operational state and receipts without changing any content table."""
    op.create_table(
        "workspace_integration_states",
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(128), nullable=False),
        sa.Column("max_valid_from", sa.DateTime(), nullable=True),
        sa.Column("max_valid_until", sa.DateTime(), nullable=True),
        sa.Column("max_evidence_ref", sa.String(128), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_integration_state_revision"),
        sa.CheckConstraint("source = 'external_billing'", name="ck_integration_state_source"),
        sa.CheckConstraint(
            "(max_valid_from IS NULL AND max_valid_until IS NULL AND max_evidence_ref IS NULL) OR "
            "(max_valid_from IS NOT NULL AND max_valid_until IS NOT NULL AND "
            "max_evidence_ref IS NOT NULL AND max_valid_until > max_valid_from)",
            name="ck_integration_state_max_window",
        ),
        if_not_exists=True,
    )
    op.create_table(
        "workspace_integration_licenses",
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace_integration_states.workspace_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("service_id", sa.String(64), primary_key=True),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_until", sa.DateTime(), nullable=False),
        sa.Column("evidence_ref", sa.String(128), nullable=False),
        sa.CheckConstraint("valid_until > valid_from", name="ck_integration_license_window"),
        if_not_exists=True,
    )
    op.create_table(
        "workspace_integration_events",
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace_integration_states.workspace_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "revision", name="uq_integration_event_revision"),
        sa.CheckConstraint("revision > 0", name="ck_integration_event_revision"),
        sa.CheckConstraint("length(payload_digest) = 64", name="ck_integration_event_digest"),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Remove only entitlement plumbing; user content and workspaces survive."""
    op.drop_table("workspace_integration_events", if_exists=True)
    op.drop_table("workspace_integration_licenses", if_exists=True)
    op.drop_table("workspace_integration_states", if_exists=True)
