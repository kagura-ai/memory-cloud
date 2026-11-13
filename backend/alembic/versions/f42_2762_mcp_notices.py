"""#2762: service notices over MCP: ``mcp_notices``, deliveries, consent history.

- ``mcp_notices``: id (UUID), text (normalised, 1-300 characters, at most 900
  UTF-8 bytes, CHECK), starts_at / expires_at (CHECK expires_at > starts_at),
  withdrawn_at, created_at. A partial index on ``expires_at`` for the rows
  that were not withdrawn serves the delivery read and the active count. No
  actor column: the writer is recorded in ``audit_logs``.
- ``mcp_notice_deliveries``: PK ``(user_id, notice_id)`` (once per user), both
  ``ON DELETE CASCADE``; an index on ``notice_id`` serves the cascade from a
  notice and the per-notice count.
- ``mcp_notice_consents``: the append-only shape of
  ``announcement_email_consents`` (#2761) in a table of its own: BIGSERIAL id,
  ``user_id`` ``ON DELETE CASCADE``, value, wording_version (required for a
  "yes"), source CHECK ``settings``, recorded_at, index ``(user_id, id)``.

New, empty tables: nothing to backfill (every user starts as "never answered",
which is off).

Downgrade drops deliveries, consents and notices, in that order (the recorded
history and the notices are lost).

Revision ID: f42_2762_mcp_notices
Revises: f41_2768_llm_call_log_lane_idx

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f42_2762_mcp_notices"
down_revision: str | None = "f41_2768_llm_call_log_lane_idx"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the three tables and their indexes."""
    op.create_table(
        "mcp_notices",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("text", sa.String(length=300), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 300 AND octet_length(text) <= 900",
            name="ck_mcp_notices_text_size",
        ),
        sa.CheckConstraint("expires_at > starts_at", name="ck_mcp_notices_window"),
    )
    op.create_index(
        "ix_mcp_notices_active",
        "mcp_notices",
        ["expires_at"],
        postgresql_where=sa.text("withdrawn_at IS NULL"),
    )

    op.create_table(
        "mcp_notice_deliveries",
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("notice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "notice_id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_mcp_notice_deliveries_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["notice_id"],
            ["mcp_notices.id"],
            name="fk_mcp_notice_deliveries_notice_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_mcp_notice_deliveries_notice_id",
        "mcp_notice_deliveries",
        ["notice_id"],
    )

    op.create_table(
        "mcp_notice_consents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Boolean(), nullable=False),
        sa.Column("wording_version", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_mcp_notice_consents_user_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("source IN ('settings')", name="valid_mcp_notice_consent_source"),
        sa.CheckConstraint(
            "value = false OR wording_version IS NOT NULL",
            name="ck_mcp_notice_consent_version_when_on",
        ),
    )
    op.create_index(
        "ix_mcp_notice_consents_user_id_id",
        "mcp_notice_consents",
        ["user_id", "id"],
    )


def downgrade() -> None:
    """Drop deliveries, consents and notices (in that order)."""
    op.drop_index("ix_mcp_notice_deliveries_notice_id", table_name="mcp_notice_deliveries")
    op.drop_table("mcp_notice_deliveries")
    op.drop_index("ix_mcp_notice_consents_user_id_id", table_name="mcp_notice_consents")
    op.drop_table("mcp_notice_consents")
    op.drop_index("ix_mcp_notices_active", table_name="mcp_notices")
    op.drop_table("mcp_notices")
