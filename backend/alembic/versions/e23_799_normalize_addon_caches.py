"""Normalize addon cache columns to the row-based SSoT (#799).

Issue #799: the ``e22_665_backfill_admin_grants`` migration created
``admin_grant`` ``WorkspaceAddon`` rows from non-zero cache columns, but
its ``WHERE admin_portion >= unit_value`` clause and floor division left
two classes of cache value **un-reconciled**:

1. **Orphans** — a legacy cache value smaller than the addon's
   ``unit_value`` (e.g. ``addon_memory_bonus=9000`` with
   ``unit_value=10000``) produces a 0-unit admin portion, so no row is
   inserted AND e22 never touched the cache column. The 9000 stays,
   violating the SSoT invariant
   ``cache == SUM(active WorkspaceAddon.quantity × unit_value)``.
2. **Partial remainders** — a non-multiple value above ``unit_value``
   (e.g. ``addon_storage_bonus_mb=250`` with ``unit_value=100``) gets a
   floor-divided 2-unit (200 MB) row, but e22 left the cache at 250.

Production impact (already mitigated): the ``kagura`` PRO workspace hit
this 2026-05-23 when the #663 admin dialog read the stale 9000 and the
post-#665 divisibility validator rejected the Save with HTTP 400. It was
manually cleared; this migration normalizes any remaining workspaces and
guarantees the invariant going forward.

### Why recalc-from-rows, not "reset non-multiples to 0"

Resetting a non-multiple cache to 0 would *re-break* the SSoT for the
partial-remainder class: ``addon_storage_bonus_mb=250`` already has a
legitimate 2-unit (200 MB) ``WorkspaceAddon`` row from e22, so cache 0 ≠
SUM 200. Instead we recompute each cache column as
``SUM(active rows) × unit_value`` for **all** active workspaces — exactly
what ``AddonCalculatorService.recalculate_workspace_bonuses``
(``services/addon_calculator_service.py:100-218``) does at runtime. This
yields ``9000 → 0`` (orphan, no row) AND ``250 → 200`` (partial, preserves
the backfilled row), and is idempotent because it writes the absolute SUM.

### Active-window predicate

The per-column SUM uses the SAME active-window predicate as the runtime
recalc (``addon_calculator_service.py:112-118``):
``active_from <= NOW() AND (active_until IS NULL OR active_until > NOW())``.
Mismatching it (e.g. summing expired/future rows) would set a cache value
that the next runtime recalc immediately contradicts — the exact subquery
trap e22's review caught (PR #797 round 3).

### Idempotency

Re-running is safe: each UPDATE writes ``SUM(active rows) × unit_value``,
an absolute value, so a second run is a no-op on already-consistent rows.

### Atomicity / lock semantics

Mirrors e22: ``LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE`` on both tables
so a concurrent admin PUT cannot race the normalization. In production the
API container is stopped during migrations (``.claude/rules/dev-environment.md``);
the locks are belt-and-suspenders for dev / manual runs. Released at the
migration's commit boundary (Alembic wraps every migration in a txn).

### Downgrade

``downgrade()`` is a deliberate no-op. The legacy non-multiple cache values
this migration overwrites are unrecoverable (we did not snapshot them), and
restoring them would re-introduce the SSoT violation. This is a forward-only
data-correction migration.

Revision ID: e23_799_normalize_addon_caches
Revises: e22_665_backfill_admin_grants
"""

from alembic import op


revision = "e23_799_normalize_addon_caches"
down_revision = "e22_665_backfill_admin_grants"
branch_labels = None
depends_on = None


# Mapping of Workspace cache column → (WorkspaceAddon.addon_type, unit_value).
# Mirrors ``e22_665``'s ``_ADDON_BACKFILL``, ``admin_plans._ADDON_FIELD_SPECS``,
# and ``ADDON_UNIT_VALUES`` in ``services/addon_calculator_service``. Kept inline
# so the migration stays self-contained and survives future refactors of the
# spec table. Adding a new addon type requires touching this list AND the spec
# table AND the recalc service — the schema-drift test fails loudly on divergence.
_ADDON_CACHE_COLUMNS: tuple[tuple[str, str, int], ...] = (
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
    """Recompute every addon cache column as SUM(active rows) × unit_value.

    Per-column ``UPDATE ... SET cache = COALESCE(SUM(active quantity), 0) *
    unit_value`` over all non-deleted workspaces. The correlated subquery's
    active-window predicate matches ``recalculate_workspace_bonuses`` exactly
    (``addon_calculator_service.py:112-118``), so the value written equals what
    the runtime recalc would compute.

    Writing the absolute SUM (not a delta) makes the statement idempotent, so
    there is no ``WHERE cache <> SUM`` guard: re-running, or running over an
    already-consistent workspace, simply rewrites the same value. Omitting the
    guard keeps the active-window predicate in a single place (one fewer copy
    to keep in sync) and avoids the SQL three-valued-logic trap where a
    ``cache <> ...`` guard would silently skip a NULL cache column. (All 9
    columns are currently ``NOT NULL DEFAULT 0``, but the guardless form is
    correct regardless.) The full-table lock is already held, so writing every
    active row rather than only drifted rows is negligible at this scale.

    The identifiers interpolated below (``cache_col``, ``addon_type``,
    ``unit_value``) come ONLY from the code-constant ``_ADDON_CACHE_COLUMNS``
    tuple — never from user input — so the f-string is safe. Column and table
    identifiers cannot be supplied as bind parameters; this mirrors the
    reviewed pattern in ``e22_665_backfill_admin_grants``.
    """
    op.execute("LOCK TABLE workspaces IN SHARE ROW EXCLUSIVE MODE")
    op.execute("LOCK TABLE workspace_addons IN SHARE ROW EXCLUSIVE MODE")

    for cache_col, addon_type, unit_value in _ADDON_CACHE_COLUMNS:
        op.execute(
            f"""
            UPDATE workspaces w
            SET {cache_col} = COALESCE((
                SELECT SUM(wa.quantity)
                FROM workspace_addons wa
                WHERE wa.workspace_id = w.id
                  AND wa.addon_type = '{addon_type}'
                  AND wa.active_from <= NOW()
                  AND (wa.active_until IS NULL OR wa.active_until > NOW())
            ), 0) * {unit_value}
            WHERE w.deleted_at IS NULL
            """
        )


def downgrade() -> None:
    """No-op: forward-only data correction.

    The legacy non-multiple cache values overwritten by ``upgrade()`` are not
    snapshotted and are unrecoverable; restoring them would re-introduce the
    SSoT violation this migration fixes. See the module docstring.
    """
