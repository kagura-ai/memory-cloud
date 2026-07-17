"""Worker app identities and composite connector dispatch key (#1315).

Creates the global app-identity control-plane table, seeds an unconfigured
``slack/default`` identity for a zero-downtime rollout, and widens connector
uniqueness from ``(platform, team_id)`` to ``(platform, app_key, team_id)``.
Existing rows are deterministically backfilled to ``default``.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e68_1315_worker_apps"
down_revision = "e67_1281_agent_ws"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_app_identities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("app_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="unconfigured", nullable=False),
        sa.Column("active_signing_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("active_secret_revision", sa.Integer(), nullable=True),
        sa.Column("retiring_signing_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("retiring_secret_revision", sa.Integer(), nullable=True),
        sa.Column("retiring_valid_until", sa.DateTime(), nullable=True),
        sa.Column("config_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "platform IN ('slack', 'discord', 'teams')",
            name="check_worker_app_identity_platform",
        ),
        sa.CheckConstraint(
            "status IN ('unconfigured', 'active', 'disabled')",
            name="check_worker_app_identity_status",
        ),
        sa.CheckConstraint(
            "app_key ~ '^[a-z0-9][a-z0-9_-]{0,63}$'",
            name="check_worker_app_identity_key",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "app_key", name="uq_worker_app_identities_platform_key"),
    )
    op.execute(
        sa.text(
            "INSERT INTO worker_app_identities "
            "(platform, app_key, display_name, status, config_version) "
            "VALUES ('slack', 'default', 'Default Slack app', 'unconfigured', 1)"
        )
    )

    op.add_column(
        "workspace_connectors",
        sa.Column("app_key", sa.String(length=64), server_default="default", nullable=True),
    )
    op.execute(sa.text("UPDATE workspace_connectors SET app_key = 'default' WHERE app_key IS NULL"))
    op.alter_column("workspace_connectors", "app_key", nullable=False)
    op.drop_index("ix_workspace_connectors_type_team", table_name="workspace_connectors")
    op.create_index(
        "ix_workspace_connectors_app_team",
        "workspace_connectors",
        ["connector_type", "app_key", "external_team_id"],
        unique=True,
    )


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT 1 FROM workspace_connectors "
            "WHERE external_team_id IS NOT NULL "
            "GROUP BY connector_type, external_team_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade #1315 while the same platform team is bound to multiple app keys"
        )

    op.drop_index("ix_workspace_connectors_app_team", table_name="workspace_connectors")
    op.create_index(
        "ix_workspace_connectors_type_team",
        "workspace_connectors",
        ["connector_type", "external_team_id"],
        unique=True,
    )
    op.drop_column("workspace_connectors", "app_key")
    op.drop_table("worker_app_identities")
