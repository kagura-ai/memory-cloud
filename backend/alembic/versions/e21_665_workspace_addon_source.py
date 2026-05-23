"""Add workspace_addons.source column + composite uniqueness (#665).

Issue #665: The admin handler ``PUT /admin/plans/workspaces/{id}/quotas``
was writing ``workspace.addon_*_bonus`` cache columns directly, bypassing
the cache-invalidation contract established in #570 (see
``AddonCalculatorService.recalculate_workspace_bonuses`` docstring).

Once Stripe addon webhooks land, every recalculate call would re-derive
the cache from ``WorkspaceAddon`` rows and silently wipe the admin's
manual grants. Fix: route admin grants through ``WorkspaceAddon`` rows so
both Stripe and admin paths share a single source of truth, then call
``recalculate_workspace_bonuses`` to refresh the cache.

To distinguish admin-granted rows from Stripe-purchased rows (so future
Stripe webhooks know which rows belong to them, and admin re-grants UPSERT
into the right row), add a ``source`` discriminator column. A composite
UNIQUE index on ``(workspace_id, addon_type, source)`` enforces "at most
one row per (workspace, addon type, provenance)" — this is what makes
admin grants UPSERT-safe (LD-2 in the gate1 review).

### Why a column instead of extending ``addon_type``

``addon_type`` already enumerates 9 SKU-like values via ``check_addon_type``.
Encoding provenance there (e.g. ``admin_extra_memory``) would double the
enum count, force every read site to strip a prefix, and tie SKU semantics
to provenance — bad SSoT design. A separate ``source VARCHAR(20)`` keeps
``addon_type`` meaning "what the units are" and ``source`` meaning "where
the row came from"; future provenances (``partner_promo``,
``support_bonus``) only add CHECK enum values, never new SKU rows.

### Why ``NOT NULL DEFAULT 'stripe'``

``NOT NULL`` blocks application code from forgetting to set ``source`` and
silently breaking the unique-key UPSERT. ``DEFAULT 'stripe'`` is the safe
historical interpretation: every row written before this migration was
written as part of the (not-yet-implemented) Stripe purchase flow scaffolding,
so back-filling them as ``'stripe'`` preserves their meaning. PG >= 11
treats ``ADD COLUMN ... NOT NULL DEFAULT <const>`` as a metadata-only
operation (no table rewrite), so the migration is fast even on large tables.

### Constraint naming

Follows the existing ``workspace_addons`` constraints (``check_addon_type``,
``check_quantity_positive``):

* ``check_addon_source`` — CHECK ``source IN ('stripe', 'admin_grant')``.
* ``uq_workspace_addons_workspace_addon_source`` — UNIQUE
  ``(workspace_id, addon_type, source)``. This is the key the admin handler
  UPSERT clauses against (``ON CONFLICT (workspace_id, addon_type, source)``).

### Downgrade

Drop the unique index, then the CHECK, then the column. Reverses cleanly;
no data loss because the upgrade only adds a discriminator that older code
paths never read.

Revision ID: e21_665_workspace_addon_source
Revises: e20_741_deprecate_edge_type
"""

from alembic import op


revision = "e21_665_workspace_addon_source"
down_revision = "e20_741_deprecate_edge_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``source`` column + CHECK + composite UNIQUE (idempotent)."""
    # 1. Add the column. NOT NULL DEFAULT 'stripe' is metadata-only on PG >= 11
    #    so existing rows are back-filled without a table rewrite. Idempotent
    #    via the same information_schema guard pattern used in #485, #560, #675.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspace_addons'
                  AND column_name = 'source'
            ) THEN
                ALTER TABLE workspace_addons
                  ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'stripe';
            END IF;
        END $$;
        """
    )

    # 2. CHECK constraint as a separate ALTER so the schema-drift detector
    #    (tests/test_schema_drift.py) picks it up. Inline column-level CHECK
    #    in ADD COLUMN is not a shape the detector parses. Wrapped in a
    #    DO ... EXCEPTION block for re-run idempotency (mirrors e15_675).
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE workspace_addons
              ADD CONSTRAINT check_addon_source
                CHECK (source IN ('stripe', 'admin_grant'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )

    # 3. Composite UNIQUE — the contract that makes admin UPSERTs safe.
    #    (workspace_id, addon_type, source) uniquely identifies "the
    #    admin-grant row for the extra_memory addon on workspace X" so
    #    the handler can ``ON CONFLICT ... DO UPDATE`` without scanning.
    #    Idempotent via DO ... EXCEPTION — ``ADD CONSTRAINT UNIQUE``
    #    raises ``duplicate_object`` on re-run (same shape as e15_675).
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE workspace_addons
              ADD CONSTRAINT uq_workspace_addons_workspace_addon_source
                UNIQUE (workspace_id, addon_type, source);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop UNIQUE, CHECK, and column (reverse order)."""
    op.execute(
        "ALTER TABLE workspace_addons "
        "DROP CONSTRAINT IF EXISTS uq_workspace_addons_workspace_addon_source"
    )
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_source")
    op.drop_column("workspace_addons", "source")
