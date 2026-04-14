"""Add global partial UNIQUE index on contexts.resource_id for active rows.

Issue #322: Zero-downtime hotfix — enforce resource_id uniqueness across
all workspaces for active (non-soft-deleted) contexts. Closes the routing
ambiguity that enabled cross-tenant Resource ingest (OWASP A01 / CWE-639).

Revision ID: a96_ctx_resource_id_unique
Revises: a95_source_uri_declared_link

NOTE: The revision ID is kept under 32 characters because
`alembic_version.version_num` is `VARCHAR(32)` in this database
(asyncpg raises `StringDataRightTruncationError` otherwise).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a96_ctx_resource_id_unique"
down_revision = "a95_source_uri_declared_link"
branch_labels = None
depends_on = None


INDEX_NAME = "ux_contexts_resource_id_active"
# Cap collision examples in the abort message: enough to give the operator
# actionable context without spamming the logs on a wide collision.
_MAX_COLLISION_EXAMPLES = 5


def upgrade() -> None:
    """Add a global partial UNIQUE index on contexts.resource_id.

    Guarantees every active (non-deleted) context row with a non-null
    resource_id is globally unique, so Resource ingest can resolve a
    resource_id to exactly one workspace.

    CREATE INDEX CONCURRENTLY cannot run inside a transaction, so we wrap
    the DDL in Alembic's autocommit_block. A prior INVALID index (from a
    partial failure) is explicitly dropped first, because IF NOT EXISTS
    would otherwise skip the CREATE and leave the database without the
    security boundary this migration is meant to establish.
    """
    # Step 1: Abort if any active duplicate resource_id values exist.
    # The partial UNIQUE index is global on resource_id, so duplicates
    # within the same workspace would also fail the CREATE INDEX step.
    # Using COUNT(*) > 1 keeps the audit aligned with the index predicate.
    # Operators must rename or remove duplicates before this migration
    # (see SECURITY.md for remediation steps).
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "SELECT resource_id, COUNT(*) AS duplicate_count "
            "FROM contexts "
            "WHERE resource_id IS NOT NULL AND deleted_at IS NULL "
            "GROUP BY resource_id "
            "HAVING COUNT(*) > 1 "
            "LIMIT :limit"
        ),
        {"limit": _MAX_COLLISION_EXAMPLES},
    )
    collisions = result.fetchall()
    if collisions:
        examples = ", ".join(f"'{row[0]}' ({row[1]} active rows)" for row in collisions)
        raise RuntimeError(
            "Migration aborted: duplicate active resource_id values found "
            f"that would violate the global unique index (examples: {examples}). "
            "Rename or remove duplicates before running this migration. "
            "See SECURITY.md for remediation steps."
        )

    # Check for a prior INVALID index from Python side — PostgreSQL forbids
    # DROP INDEX CONCURRENTLY inside DO / function bodies, so the INVALID
    # detection must live out here where it can issue a plain DDL next.
    invalid_index_result = bind.execute(
        sa.text(
            "SELECT 1 "
            "FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = :index_name "
            "AND NOT i.indisvalid "
            "LIMIT 1"
        ),
        {"index_name": INDEX_NAME},
    )
    has_invalid_index = invalid_index_result.fetchone() is not None

    # autocommit_block lets us issue DDL outside Alembic's transaction so
    # CONCURRENTLY is accepted.
    with op.get_context().autocommit_block():
        # INVALID indexes from a prior failed CONCURRENTLY run would bypass
        # the IF NOT EXISTS check on CREATE and leave us unprotected.
        if has_invalid_index:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))
        op.execute(
            sa.text(
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
                "ux_contexts_resource_id_active "
                "ON contexts (resource_id) "
                "WHERE resource_id IS NOT NULL AND deleted_at IS NULL"
            )
        )


def downgrade() -> None:
    """Drop the partial unique index (tolerant of missing/invalid state)."""
    op.execute(sa.text("DROP INDEX IF EXISTS ux_contexts_resource_id_active"))
