"""Per-workspace embedding spend cap (#709).

Issue #709: prereq for #708 Option A (shared-context reads charge the
shared-source workspace owner's BYOK key). Without a per-workspace ceiling,
adopting that policy opens an "Embedding Drain Attack" — a hostile actor
seeds a high-token context, shares it widely, and burns the owner's API
budget on every read.

This migration adds two nullable USD columns on ``workspaces``:

1. ``embedding_daily_cap_usd`` — admin-set daily ceiling on BYOK embedding
   spend. ``NULL`` = "inherit tier default" (set on ``PlanTier``); non-NULL
   = "override applied". Range non-negative enforced by CHECK constraint.
2. ``embedding_monthly_cap_usd`` — same semantics, monthly window.

This is an **override** column, not an addon — admin-set values REPLACE
the tier default rather than stacking on top of it (the cap is a ceiling,
not a quota that addons expand). That is why no new entry is added to
``workspace_addons.check_addon_type``; the existing ``addon_*`` columns on
``Workspace`` are reserved for additive quotas (#15, #485, #560, #238).

Idempotency:
    Same ``information_schema.columns`` guard as #15 / #485 / #560 / #675.
    CHECK constraints wrapped in ``DO ... EXCEPTION WHEN duplicate_object``
    so re-running after a partial-success run is a no-op.

Aggregation path:
    Hot-path enforcement reads a Redis counter (see
    ``services/embedding_spend_cap_service.py``). DB aggregation over
    ``llm_call_log`` is reserved for the admin "current spend" panel and
    relies on the existing ``idx_llm_call_log_workspace_period`` composite
    index on ``(workspace_id, occurred_at)``; no new index needed.

Revision ID: e16_709_embedding_spend_cap  (28 chars — within VARCHAR(32))
Revises: e15_675_workspace_slot_bonus
"""

from alembic import op

revision = "e16_709_embedding_spend_cap"
down_revision = "e15_675_workspace_slot_bonus"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Daily cap column (idempotent).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspaces'
                  AND column_name = 'embedding_daily_cap_usd'
            ) THEN
                ALTER TABLE workspaces
                  ADD COLUMN embedding_daily_cap_usd NUMERIC(10, 6);
            END IF;
        END $$;
        """
    )

    # 2. Monthly cap column (idempotent).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspaces'
                  AND column_name = 'embedding_monthly_cap_usd'
            ) THEN
                ALTER TABLE workspaces
                  ADD COLUMN embedding_monthly_cap_usd NUMERIC(10, 6);
            END IF;
        END $$;
        """
    )

    # 3. Non-negative CHECK on daily cap (NULL-permissive — NULL means
    #    "inherit tier default", not "negative"). Idempotent via the
    #    same ``EXCEPTION WHEN duplicate_object`` shape as #675.
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE workspaces
              ADD CONSTRAINT embedding_daily_cap_usd_nonneg
                CHECK (embedding_daily_cap_usd IS NULL OR embedding_daily_cap_usd >= 0);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )

    # 4. Non-negative CHECK on monthly cap.
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE workspaces
              ADD CONSTRAINT embedding_monthly_cap_usd_nonneg
                CHECK (embedding_monthly_cap_usd IS NULL OR embedding_monthly_cap_usd >= 0);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop both CHECK constraints, then drop both columns.

    Override values are not preserved on downgrade — admins re-apply via
    the admin /plans UI after re-upgrade.
    """
    op.execute("ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS embedding_monthly_cap_usd_nonneg")
    op.execute("ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS embedding_daily_cap_usd_nonneg")
    op.drop_column("workspaces", "embedding_monthly_cap_usd")
    op.drop_column("workspaces", "embedding_daily_cap_usd")
