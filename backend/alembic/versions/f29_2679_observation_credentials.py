"""Scoped read-only observation credentials for the observability API (#2679)."""

import sqlalchemy as sa

from alembic import op

revision = "f29_2679_observation_credentials"
down_revision = "f28_2678_public_mcp_usage_events"
branch_labels = None
depends_on = None

# Inlined from ``utils.public_id.public_id_server_default(PublicIdPrefix.OBSERVATION_KEY)``
# so the migration does not change if the helper does.
_PUBLIC_ID_DEFAULT = "'obsk_' || substr(replace((gen_random_uuid())::text, '-', ''), 1, 22)"


def upgrade() -> None:
    op.create_table(
        "observation_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "public_id",
            sa.String(32),
            nullable=False,
            server_default=sa.text(_PUBLIC_ID_DEFAULT),
        ),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workspaces.id",
                ondelete="CASCADE",
                name="fk_observation_credentials_workspace_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "resource_pk",
            sa.Uuid(),
            sa.ForeignKey(
                "resources.id",
                ondelete="CASCADE",
                name="fk_observation_credentials_resource_pk",
            ),
            nullable=True,
        ),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "rotated_from_id",
            sa.Integer(),
            sa.ForeignKey(
                "observation_credentials.id",
                ondelete="SET NULL",
                name="fk_observation_credentials_rotated_from_id",
            ),
            nullable=True,
        ),
        sa.UniqueConstraint("public_id", name="observation_credentials_public_id_key"),
        sa.UniqueConstraint("key_hash", name="observation_credentials_key_hash_key"),
        sa.CheckConstraint(
            "scope IN ('workspace', 'resource')",
            name="ck_observation_credentials_scope",
        ),
        sa.CheckConstraint(
            "(scope = 'resource') = (resource_pk IS NOT NULL)",
            name="ck_observation_credentials_scope_resource",
        ),
    )
    op.create_index(
        "ix_observation_credentials_workspace_id", "observation_credentials", ["workspace_id"]
    )
    op.create_index(
        "ix_observation_credentials_expires_at", "observation_credentials", ["expires_at"]
    )
    op.create_index(
        "ix_observation_credentials_revoked_at", "observation_credentials", ["revoked_at"]
    )


def downgrade() -> None:
    op.drop_table("observation_credentials")
