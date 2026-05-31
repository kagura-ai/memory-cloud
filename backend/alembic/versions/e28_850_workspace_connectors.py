"""Add workspace_connectors table — ai-worker chat-ingest connector profiles.

Issue #850 (F6-a of epic #755, RFC-0001). The pure-backend schema foundation
for ai-worker chat-ingest connectors, reusing memory-cloud's Resource
Foundation. Net-new table only — the setup flow, seat-cap enforcement, and
connector-scoped token minting land in F6-b; this migration is schema only.

Design notes:
- 1:1 with ``resources``: ``resource_pk`` is a NOT NULL FK to ``resources.id``
  with a UNIQUE constraint. A new table has no backfill window, so
  ``resource_pk`` is NOT NULL from creation — the Phase-1 nullable shadow
  pattern used by the existing resource satellites (a97/#323) does not apply.
- No ``resource_id`` slug column: the connector links purely by the
  ``resource_pk`` UUID, sidestepping the CWE-639 slug-reuse class entirely, so
  it is intentionally NOT hooked into the ORM ``_enforce_resource_pk_invariant``
  before_insert listener (see models/resource.py).
- ``oauth_tokens_encrypted`` stores Fernet ciphertext (utils/encryption.py);
  plaintext OAuth tokens are never persisted.
- ``workspace_id`` is denormalized for filter parity with the other resource
  tables and CASCADE-deletes with the workspace.
- Timestamps are naive ``DateTime`` to match the resource-table family; UTC is
  guaranteed at the engine/container layer (see .claude/rules/backend.md).
- The ``check_connector_type`` CHECK is kept byte-identical to the ORM
  ``WorkspaceConnector.__table_args__`` so test_schema_drift.py stays green.

Revision ID: e28_850_workspace_connectors
Revises: e27_805_drop_ws_memory_limit
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# NOTE: revision id kept <= 32 chars — alembic_version.version_num is VARCHAR(32).
revision: str = "e28_850_workspace_connectors"
down_revision: str | Sequence[str] | None = "e27_805_drop_ws_memory_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the workspace_connectors table (1:1 with resources)."""
    op.create_table(
        "workspace_connectors",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "resource_pk",
            UUID(as_uuid=True),
            sa.ForeignKey("resources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connector_type", sa.String(20), nullable=False),
        # Fernet ciphertext of the OAuth token bundle (NULL until F6-b writes).
        sa.Column("oauth_tokens_encrypted", sa.Text, nullable=True),
        sa.Column("pii_guardrail_config", JSONB, nullable=True),
        sa.Column("litellm_virtual_key_id", sa.String(255), nullable=True),
        sa.Column(
            "config_version",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("virtual_key_valid_until", sa.DateTime, nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        # 1:1 connector -> resource. UNIQUE on the FK column is what enforces it.
        sa.UniqueConstraint("resource_pk", name="uq_workspace_connectors_resource_pk"),
        sa.CheckConstraint(
            "connector_type IN ('slack', 'discord', 'teams')",
            name="check_connector_type",
        ),
    )

    # Workspace-scoped lookups (filter parity with the other resource tables).
    op.create_index(
        "ix_workspace_connectors_workspace_id",
        "workspace_connectors",
        ["workspace_id"],
    )


def downgrade() -> None:
    """Drop the workspace_connectors table."""
    op.drop_index("ix_workspace_connectors_workspace_id", table_name="workspace_connectors")
    op.drop_table("workspace_connectors")
