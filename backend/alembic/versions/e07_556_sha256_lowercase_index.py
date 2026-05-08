"""Case-insensitive sha256 dedup index (#556 follow-up).

Issue #556 added service-layer lowercase normalization of ``sha256`` in
``FileStorageService.reserve_upload`` / ``confirm_upload``. The DB-level
partial unique index ``uq_file_objects_workspace_sha256_active`` was
still on the raw ``sha256`` column (case-sensitive), which left a
narrow regression: if a tenant had any pre-merge active rows with
upper-case sha256 (REST path with no normalization, mixed-case clients),
a post-merge upload of the same digest would lowercase it and slip past
the case-sensitive index, creating a duplicate that defeats per-workspace
dedup.

This migration:

1. Aborts loud if any ``(workspace_id, lower(sha256))`` group has 2+
   active rows. The operator must resolve those manually before the
   migration can proceed (the lowercase normalization step would
   otherwise create a unique-index collision and roll back).
2. Normalizes all ``file_objects.sha256`` values to lowercase.
3. Drops the case-sensitive partial unique index and replaces it with a
   functional index on ``lower(sha256)``. The service-layer normalization
   keeps stored values lowercase by construction; the functional index
   is defense-in-depth in case any non-service write path drifts.

Downgrade restores the case-sensitive index but does NOT restore the
original casings (we do not snapshot them).

Revision ID: e07_556_sha256_lowercase_index
Revises: e06_560_sleep_quota_addon
"""

import sqlalchemy as sa

from alembic import op

revision = "e07_556_sha256_lowercase_index"
down_revision = "e06_560_sleep_quota_addon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Pre-check: any (workspace_id, lower(sha256)) groups with multiple
    #    active rows would collide once we lowercase. Refuse to proceed.
    op.execute(
        """
        DO $$
        DECLARE
            dup_count INTEGER;
            sample TEXT;
        BEGIN
            SELECT COUNT(*) INTO dup_count FROM (
                SELECT workspace_id, lower(sha256) AS sha_lower
                FROM file_objects
                WHERE deleted_at IS NULL AND status <> 'failed'
                GROUP BY workspace_id, lower(sha256)
                HAVING COUNT(*) > 1
            ) AS d;
            IF dup_count > 0 THEN
                SELECT string_agg(
                    format('workspace=%s sha_lower=%s ids=%s',
                           workspace_id, sha_lower, ids),
                    '; '
                ) INTO sample
                FROM (
                    SELECT
                        workspace_id,
                        lower(sha256) AS sha_lower,
                        array_agg(id) AS ids
                    FROM file_objects
                    WHERE deleted_at IS NULL AND status <> 'failed'
                    GROUP BY workspace_id, lower(sha256)
                    HAVING COUNT(*) > 1
                    LIMIT 20
                ) AS d;
                RAISE EXCEPTION
                  'Migration e07_556 found % case-collision (workspace_id, lower(sha256)) groups. '
                  'Resolve manually (soft-delete the duplicate(s) you do not want to keep) '
                  'before re-running. Sample: %', dup_count, sample;
            END IF;
        END $$;
        """
    )

    # 2. Normalize all stored sha256 values to lowercase. Idempotent — only
    #    rows where the normalized form differs are touched. The case-
    #    sensitive partial unique index is still in place at this point;
    #    it will not collide because step 1 already verified there is at
    #    most one active row per ``(workspace_id, lower(sha256))`` group.
    op.execute("UPDATE file_objects SET sha256 = lower(sha256) WHERE sha256 <> lower(sha256)")

    # 3. Replace the case-sensitive partial unique index with a functional
    #    one that uses lower(sha256). Service-layer normalization makes
    #    this functionally redundant in practice, but it is defense-in-
    #    depth against any write path that bypasses the service.
    op.drop_index("uq_file_objects_workspace_sha256_active", table_name="file_objects")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_file_objects_workspace_sha256_active
        ON file_objects (workspace_id, lower(sha256))
        WHERE deleted_at IS NULL AND status <> 'failed'
        """
    )


def downgrade() -> None:
    # Reverse the index swap. Data normalization is one-way — original
    # casings are not snapshotted, so this only restores index behavior.
    op.drop_index("uq_file_objects_workspace_sha256_active", table_name="file_objects")
    op.create_index(
        "uq_file_objects_workspace_sha256_active",
        "file_objects",
        ["workspace_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND status <> 'failed'"),
    )
