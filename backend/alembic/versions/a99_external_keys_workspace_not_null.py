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
    # Defensive pre-check #1: surface a clear error if any legacy NULL workspace_id
    # rows remain. Phase 1 (#381) deleted /external-keys/import (the only producer of
    # NULL workspace_id rows), and the owner confirmed pre-#146 data is gone
    # (2026-04-19), but we don't want a stale staging DB to fail with a cryptic
    # IntegrityError on the NOT NULL alter. Pattern mirrors a97_resources_entity's
    # pre-flight audit.
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

    # Defensive pre-check #2: the partial unique index would fail if any
    # (workspace_id, provider) pair already has 2+ enabled rows. Pre-#385 the
    # invariant was per-user (different users in the same workspace could each have
    # their own enabled provider key), so legacy data could violate. Surface a
    # clear remediation path instead of a cryptic CREATE INDEX failure.
    dup_rows = conn.execute(
        sa.text(
            "SELECT workspace_id, provider, COUNT(*) AS cnt "
            "FROM external_api_keys "
            "WHERE enabled = true "
            "GROUP BY workspace_id, provider "
            "HAVING COUNT(*) > 1 "
            "ORDER BY cnt DESC "
            "LIMIT 5"
        )
    ).fetchall()
    if dup_rows:
        examples = ", ".join(
            f"workspace={row[0]} provider={row[1]} ({row[2]} enabled)" for row in dup_rows
        )
        raise RuntimeError(
            "Migration aborted: external_api_keys has multiple enabled rows for the "
            f"same (workspace_id, provider) pair (examples: {examples}). The new "
            "partial unique index uq_external_api_keys_workspace_provider_enabled "
            "requires at most one enabled key per (workspace, provider). Disable or "
            "delete the duplicates before re-running this migration."
        )

    # Defensive pre-check #3: reranker exclusivity is now enforced per-workspace
    # (Cohere XOR Voyage). Pre-#385 it was per-user, so a workspace could legitimately
    # have both providers enabled across different users. The new partial unique index
    # only covers same-provider duplicates, not cross-provider reranker conflicts, so
    # those would slip past the schema and leave reranker_service picking a key
    # nondeterministically. Abort with a clear remediation path.
    reranker_conflicts = conn.execute(
        sa.text(
            "SELECT workspace_id, "
            "       COUNT(*) AS cnt, "
            "       STRING_AGG(provider, ', ' ORDER BY provider) AS providers "
            "FROM external_api_keys "
            "WHERE enabled = true "
            "  AND provider IN ('cohere', 'voyage') "
            "GROUP BY workspace_id "
            "HAVING COUNT(*) > 1 "
            "ORDER BY cnt DESC, workspace_id "
            "LIMIT 5"
        )
    ).fetchall()
    if reranker_conflicts:
        examples = ", ".join(
            f"workspace={row[0]} providers=[{row[2]}] ({row[1]} enabled reranker keys)"
            for row in reranker_conflicts
        )
        raise RuntimeError(
            "Migration aborted: some workspaces have multiple enabled reranker keys "
            f"across Cohere/Voyage (examples: {examples}). The new per-workspace "
            "reranker invariant requires choosing exactly one enabled reranker "
            "provider per workspace. Disable or delete the extra Cohere/Voyage "
            "keys before re-running this migration."
        )

    # Defensive pre-check #4: the update/toggle/delete handlers look up rows by
    # (workspace_id, key_name) via scalar_one_or_none(), which would raise
    # MultipleResultsFound (→ HTTP 500) if any legacy row pair shares the same
    # (workspace_id, key_name). Pre-#381 multiple users could each create a key
    # named "openai_primary" in the same workspace — schema allowed it because
    # uniqueness was per-user. Detect and abort before creating the unique index
    # below.
    name_dupes = conn.execute(
        sa.text(
            "SELECT workspace_id, key_name, COUNT(*) AS cnt "
            "FROM external_api_keys "
            "GROUP BY workspace_id, key_name "
            "HAVING COUNT(*) > 1 "
            "ORDER BY cnt DESC, workspace_id, key_name "
            "LIMIT 5"
        )
    ).fetchall()
    if name_dupes:
        examples = ", ".join(
            f"workspace={row[0]} key_name='{row[1]}' ({row[2]} rows)" for row in name_dupes
        )
        raise RuntimeError(
            "Migration aborted: external_api_keys has multiple rows sharing the same "
            f"(workspace_id, key_name) pair (examples: {examples}). The new "
            "unique index uq_external_api_keys_workspace_key_name requires names "
            "to be unique within a workspace. Rename or delete the duplicates "
            "before re-running this migration."
        )

    op.create_index(
        "uq_external_api_keys_workspace_provider_enabled",
        "external_api_keys",
        ["workspace_id", "provider"],
        unique=True,
        postgresql_where=sa.text("enabled = true"),
    )

    op.create_index(
        "uq_external_api_keys_workspace_key_name",
        "external_api_keys",
        ["workspace_id", "key_name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_external_api_keys_workspace_key_name",
        table_name="external_api_keys",
    )
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
