"""Add signup_gate_config + signup_allowlist tables.

Issue #358 Phase 1: admin-configurable signup gate (manual allowlist mode).

Sponsors-related columns on ``signup_gate_config`` (account, token/webhook
secret encrypted, min tier, last_sync_at) are reserved here but unused in
Phase 1; Phase 2 activates them without a further migration. Default seed
row has ``enabled=false`` so OSS behavior is preserved — the existing
``_check_registration_allowed`` / ``ALLOW_REGISTRATION`` path stays
authoritative until an admin flips the toggle.

Revision ID: b04_358_signup_gate
Revises: b03_396_edges_ws_ctx_not_null

NOTE: Revision IDs are capped at 32 chars because ``alembic_version.version_num``
is ``VARCHAR(32)`` in this database (asyncpg raises
``StringDataRightTruncationError`` otherwise).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "b04_358_signup_gate"
down_revision = "b03_396_edges_ws_ctx_not_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create signup_gate_config (singleton) + signup_allowlist tables."""
    op.create_table(
        "signup_gate_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("mode", sa.String(20), nullable=False, server_default="manual"),
        # Phase 2 (reserved; nullable so Phase 1 admins never see them populated):
        sa.Column("github_sponsors_account", sa.String(255), nullable=True),
        sa.Column("github_sponsors_token_encrypted", sa.Text(), nullable=True),
        sa.Column("github_sponsors_webhook_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("github_sponsors_min_tier_cents", sa.Integer(), nullable=True),
        sa.Column(
            "github_sponsors_grace_period_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "mode IN ('manual', 'github_sponsors', 'both')",
            name="valid_signup_gate_mode",
        ),
        # Enforce the singleton invariant at the schema layer so misuse (e.g.
        # accidentally inserting a second row via raw SQL) fails loudly rather
        # than silently creating drift between two config states.
        sa.CheckConstraint("id = 1", name="signup_gate_config_singleton"),
    )

    op.create_table(
        "signup_allowlist",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # GitHub numeric user ID stored as string (no integer-size ceiling, and
        # matches how OAuth "sub" claims are handled elsewhere in this codebase).
        sa.Column("github_user_id", sa.String(64), nullable=False),
        sa.Column("github_username", sa.String(255), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("added_by_user_id", sa.String(255), nullable=True),
        # Phase 2 fields (reserved; populated by Sponsors sync later):
        sa.Column("sponsor_tier_cents", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("grace_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "source IN ('manual', 'github_sponsors')",
            name="valid_signup_allowlist_source",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'grace', 'revoked')",
            name="valid_signup_allowlist_state",
        ),
        # Same GitHub user can legitimately exist in both `manual` and
        # `github_sponsors` (an admin may manually whitelist someone who later
        # becomes a sponsor). (github_user_id, source) is the natural key.
        sa.UniqueConstraint("github_user_id", "source", name="uq_allowlist_user_source"),
    )
    op.create_index(
        "ix_signup_allowlist_github_user_id",
        "signup_allowlist",
        ["github_user_id"],
    )

    # Seed the singleton config row with defaults (enabled=false preserves OSS
    # behavior). SignupGateService._load_config() also has a safety net that
    # inserts the row on first access, but seeding here keeps admin UI GETs
    # fast and avoids a surprising write-on-read.
    op.execute("INSERT INTO signup_gate_config (id, enabled, mode) VALUES (1, false, 'manual')")


def downgrade() -> None:
    """Drop both tables (config singleton row vanishes with the table)."""
    op.drop_index("ix_signup_allowlist_github_user_id", table_name="signup_allowlist")
    op.drop_table("signup_allowlist")
    op.drop_table("signup_gate_config")
