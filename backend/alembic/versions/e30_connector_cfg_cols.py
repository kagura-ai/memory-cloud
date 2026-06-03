"""Add connector-registration config columns to ``workspace_connectors``.

Spec 2026-06-02 (connector app registration, Plan 2): the self-serve
registration flow + the worker ``/api/v1/workers/config`` endpoint need
per-connector configuration that the F6-a schema-only slice did not carry:

- ``context_id`` — write-target context (path a). FK to ``contexts.id``
  ``ON DELETE SET NULL`` so deleting a context does not orphan the connector
  row; the worker config endpoint treats a NULL target as not-ready.
- ``locale`` — worker pre-compile locale (defaults to workspace locale).
- ``channel_ids`` — Slack channel selection (v1 = id list only; JSONB).
- ``llm_config_encrypted`` — Fernet ciphertext of the BYO LLM bundle
  ({provider, model, api_key}).
- ``kmc_api_key_encrypted`` — Fernet ciphertext of the workspace-scoped KMC
  write key. The ``api_keys`` row stores only the SHA256 hash for auth; this
  column holds the encrypted plaintext so the worker can fetch it on every
  config read (the api_keys plaintext-visibility window auto-expires and is
  unsuitable for long-term retrieval).

All columns are nullable so existing connector rows upgrade without backfill.

Revision ID: e30_connector_cfg_cols
Revises: e29_619_memories_ws_ctx_idx
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# NOTE: revision id kept <= 32 chars — alembic_version.version_num is VARCHAR(32).
revision = "e30_connector_cfg_cols"
down_revision = "e29_619_memories_ws_ctx_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_connectors",
        sa.Column(
            "context_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "workspace_connectors",
        sa.Column("locale", sa.String(10), nullable=True),
    )
    # Queryable external account/team identifier (Slack team_id / Discord guild
    # id / Teams tenant) — the worker config endpoint dispatches by this. Lives
    # in a column (not the encrypted oauth bundle) so it can be filtered in SQL.
    op.add_column(
        "workspace_connectors",
        sa.Column("external_team_id", sa.String(255), nullable=True),
    )
    # UNIQUE: one platform team → exactly one connector (no cross-tenant
    # dispatch hijack). NULL external_team_id rows are exempt (Postgres allows
    # multiple NULLs in a unique index).
    op.create_index(
        "ix_workspace_connectors_type_team",
        "workspace_connectors",
        ["connector_type", "external_team_id"],
        unique=True,
    )
    op.add_column(
        "workspace_connectors",
        sa.Column("channel_ids", JSONB, nullable=True),
    )
    op.add_column(
        "workspace_connectors",
        sa.Column("llm_config_encrypted", sa.Text, nullable=True),
    )
    op.add_column(
        "workspace_connectors",
        sa.Column("kmc_api_key_encrypted", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspace_connectors", "kmc_api_key_encrypted")
    op.drop_column("workspace_connectors", "llm_config_encrypted")
    op.drop_column("workspace_connectors", "channel_ids")
    op.drop_index("ix_workspace_connectors_type_team", table_name="workspace_connectors")
    op.drop_column("workspace_connectors", "external_team_id")
    op.drop_column("workspace_connectors", "locale")
    op.drop_column("workspace_connectors", "context_id")
