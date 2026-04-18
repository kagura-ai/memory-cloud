"""Issue #385: enforce workspace_id NOT NULL on external_api_keys + partial unique index.

Phase 2 of the external API keys workspace authorization rework. Phase 1 (#381) removed
the legacy POST /external-keys/import endpoint (the sole source of workspace_id=NULL rows)
and confirmed that no legacy pre-#146 data remains. This migration tightens the schema:

1. Drop the historical NULL allowance on workspace_id — every external API key now belongs
   to exactly one workspace, never to a "global / personal" scope.
2. Enforce at most one enabled key per (workspace, provider) pair via a partial unique
   index. Disabled keys are intentionally exempt so an owner can hold a "spare" key for a
   provider in a disabled state without conflicting with the active one.

Revision ID: a99_ext_keys_ws_not_null
Revises: a98_bm25_idf_drift_log
Create Date: 2026-04-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a99_ext_keys_ws_not_null"
down_revision: str | Sequence[str] | None = "a98_bm25_idf_drift_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Defensive pre-check: surface a clear error if any legacy NULL workspace_id rows
    # remain. Phase 1 (#381) deleted /external-keys/import (the only producer of NULL
    # workspace_id rows), and the owner confirmed pre-#146 data is gone (2026-04-19),
    # but we don't want a stale staging DB to fail with a cryptic IntegrityError on
    # the NOT NULL alter. Pattern mirrors a97_resources_entity's pre-flight audit.
    conn = op.get_bind()
    null_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM external_api_keys WHERE workspace_id IS NULL")
    ).scalar()
    if null_count and null_count > 0:
        raise RuntimeError(
            f"Migration aborted: {null_count} external_api_keys row(s) have NULL "
            "workspace_id. Phase 1 (#381) was supposed to leave none. Either "
            "backfill the workspace_id column or delete the legacy rows before "
            "re-running this migration."
        )

    op.alter_column(
        "external_api_keys",
        "workspace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    op.create_index(
        "uq_external_api_keys_workspace_provider_enabled",
        "external_api_keys",
        ["workspace_id", "provider"],
        unique=True,
        postgresql_where=sa.text("enabled = true"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_external_api_keys_workspace_provider_enabled",
        table_name="external_api_keys",
    )
    op.alter_column(
        "external_api_keys",
        "workspace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
