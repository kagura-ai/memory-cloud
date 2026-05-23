"""Backfill pre-#665 admin grants into WorkspaceAddon rows (#665).

Issue #665 review-finding #1: e21_665 added the ``source`` column +
``UNIQUE`` constraint that makes admin grants UPSERT-safe, but did not
backfill any ``WorkspaceAddon`` rows for cache values that the pre-#665
admin handler had written directly to ``Workspace.addon_*_bonus``.

Without this migration, a typical post-deploy sequence would be:

1. Workspace W has ``addon_memory_bonus=30000`` from a pre-#665 admin grant
   (legacy handler wrote the cache column directly; no
   ``WorkspaceAddon`` row exists).
2. Any later call to ``AddonCalculatorService.recalculate_workspace_bonuses``
   for W — e.g. a future Stripe webhook, OR an admin PUT touching an
   unrelated addon — SUMs zero matching rows for ``extra_memory`` and
   resets ``addon_memory_bonus`` to 0.
3. The admin's pre-#665 grant is silently wiped.

This migration walks every workspace and every ``addon_*_bonus`` column
with a non-zero value, and INSERTs a corresponding
``WorkspaceAddon(source='admin_grant', quantity=bonus/unit_value)`` row.
``ON CONFLICT DO NOTHING`` guards the rare case of a pre-existing row at
the composite UNIQUE.

### Divisibility loss

If a legacy cache value is not a multiple of the addon's ``unit_value``
(e.g. ``addon_storage_bonus_mb=250`` with ``unit_value=100``), we use
integer floor division. The backfilled row covers ``2 × 100 = 200 MB``
and the recalc that follows on the next mutation drops the cache from
250 to 200. This 50-MB loss is intentional and aligned with the post-#665
contract that values must be multiples of ``unit_value`` (enforced by
the admin handler's divisibility check). The legacy non-multiple values
are a small population and the rounding-down direction errs toward
preserving "at most what the admin granted".

### Idempotency

The migration is idempotent on re-run because every INSERT carries
``ON CONFLICT (workspace_id, addon_type, source) DO NOTHING``. Re-running
after a partial-success or after admins have manually adjusted grants
in the new path leaves their adjustments intact.

### Atomicity / lock semantics

The migration acquires ``LOCK TABLE workspaces IN SHARE ROW EXCLUSIVE
MODE`` and ``LOCK TABLE workspace_addons IN SHARE ROW EXCLUSIVE MODE``
to prevent a concurrent INSERT from a brand-new admin PUT path racing
with the backfill. The locks are released at the migration's commit
boundary (Alembic wraps every migration in a transaction).

In production, per ``.claude/rules/dev-environment.md``, the API container
is stopped during migrations, so there is no live admin-PUT path to race.
The locks are belt-and-suspenders correctness for dev / manual runs.

### Downgrade

The downgrade DELETEs admin_grant rows that the upgrade inserted. We
cannot perfectly distinguish "backfill-inserted admin_grant row" from
"post-deploy admin-created admin_grant row" without a discriminator
column. The downgrade therefore deletes ALL admin_grant rows — which is
acceptable because a downgrade is reverting to the pre-#665 cache-write
behavior, and any post-deploy admin grants would have been written
ONLY as WorkspaceAddon rows that the downgraded code can't read anyway.
Operators downgrading should be aware that this is a destructive
revert of admin-grant history.

Revision ID: e22_665_backfill_admin_grants
Revises: e21_665_workspace_addon_source
"""

from alembic import op


revision = "e22_665_backfill_admin_grants"
down_revision = "e21_665_workspace_addon_source"
branch_labels = None
depends_on = None


# Mapping of Workspace cache column → (WorkspaceAddon.addon_type, unit_value)
# Mirrors ``admin_plans._ADDON_FIELD_SPECS`` and ``ADDON_UNIT_VALUES`` from
# ``services/addon_calculator_service``. Kept inline here (instead of imported)
# so the migration is self-contained and survives future refactors of the spec
# table. Adding a new addon type requires touching this list AND the spec table
# AND the recalc service — schema-drift test will fail loudly if they diverge.
_ADDON_BACKFILL: tuple[tuple[str, str, int], ...] = (
    ("addon_memory_bonus", "extra_memory", 10000),
    ("addon_mcp_quota_bonus", "extra_mcp_quota", 5000),
    ("addon_rest_quota_bonus", "extra_rest_quota", 1000),
    ("addon_public_quota_bonus", "extra_public_quota", 500),
    ("addon_member_bonus", "extra_members", 5),
    ("addon_context_bonus", "extra_contexts", 5),
    ("addon_analysis_bonus", "extra_analysis_runs", 1),
    ("addon_storage_bonus_mb", "extra_storage", 100),
    ("addon_sleep_contexts_bonus", "extra_sleep_contexts", 1),
)


def upgrade() -> None:
    """Insert admin_grant rows for every non-zero cache column on every workspace.

    Per-column INSERT ... SELECT with floor-divide and a positive-only WHERE
    filter (``quantity > 0`` ensures we never insert a zero-quantity row,
    which would violate ``check_quantity_positive``). ``ON CONFLICT DO
    NOTHING`` on the composite UNIQUE makes the migration idempotent.
    """
    op.execute("LOCK TABLE workspaces IN SHARE ROW EXCLUSIVE MODE")
    op.execute("LOCK TABLE workspace_addons IN SHARE ROW EXCLUSIVE MODE")

    for cache_col, addon_type, unit_value in _ADDON_BACKFILL:
        op.execute(
            f"""
            INSERT INTO workspace_addons
                (workspace_id, addon_type, quantity, source, active_from, created_by)
            SELECT
                w.id,
                '{addon_type}',
                w.{cache_col} / {unit_value},
                'admin_grant',
                NOW(),
                'pre_665_migration_backfill'
            FROM workspaces w
            WHERE w.{cache_col} >= {unit_value}
              AND w.deleted_at IS NULL
            ON CONFLICT ON CONSTRAINT uq_workspace_addons_workspace_addon_source
                DO NOTHING
            """
        )


def downgrade() -> None:
    """Remove backfilled admin_grant rows.

    This is intentionally aggressive: it deletes ALL admin_grant rows, not
    just rows where ``created_by='pre_665_migration_backfill'``. Rationale
    is in the module docstring — a downgrade is reverting to pre-#665
    cache-write semantics, so post-deploy admin grants would be ghosts in
    the downgraded world anyway.
    """
    op.execute(
        """
        DELETE FROM workspace_addons
        WHERE source = 'admin_grant'
        """
    )
