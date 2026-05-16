"""Add users.workspace_slot_bonus with grandfather backfill (#675).

Issue #675 (epic #674 sub-A): replace the plan-tier-derived workspace cap
with a per-user ``workspace_slot_bonus`` column. The effective cap becomes
``1 (base) + users.workspace_slot_bonus`` rather than
``PlanTier.max_owned_workspaces`` derived from the highest-tier owned
workspace.

This migration is foundation-only:

1. Adds ``users.workspace_slot_bonus`` (INTEGER NOT NULL DEFAULT 0,
   idempotent via the same ``information_schema`` guard used in #485 /
   #15 / #560).
2. Grandfathers existing users so nobody loses a workspace when the new
   cap goes live: each user with N>1 owned (non-deleted) workspaces gets
   ``workspace_slot_bonus = N - 1``, giving them an effective cap of
   ``1 + (N-1) = N`` — exactly their current count.

Lock semantics:
    ``LOCK TABLE users IN EXCLUSIVE MODE`` is held inside the Alembic
    transaction (``env.py`` wraps every migration in
    ``context.begin_transaction()``; no ``transactional_ddl=False``).
    The lock is released at commit. Concurrent ``INSERT INTO workspaces``
    that would change ``owned_count`` between the sub-SELECT and the
    write is blocked for the duration of the migration.

Idempotency:
    The column-add guard makes a re-run after partial failure a no-op,
    and the grandfather UPDATE is itself idempotent: re-applying
    ``GREATEST(0, owned_count - 1)`` against unchanged workspace state
    converges to the same value. (Repeated runs after workspace state
    has changed will recompute — this is intentional, but the migration
    only runs once because the alembic_version write commits with the
    same transaction.)

Soft-delete:
    ``users`` has no ``deleted_at`` column (only ``workspaces`` and
    ``contexts`` do). The outer UPDATE intentionally targets all user
    rows; the soft-delete filter belongs only on the ``workspaces``
    sub-SELECT, matching ``plan_resolver.get_user_workspace_cap_summary``
    runtime semantics post-#675.

Join key:
    ``workspaces.owner_user_id`` is a VARCHAR carrying the OAuth ``sub``
    claim, NOT the integer ``users.id`` primary key. The join condition
    is ``workspaces.owner_user_id = users.user_id``.

Downgrade:
    Drops the column. Bonus values are NOT preserved — they are
    recomputable from live workspace state on re-upgrade.

Revision ID: e15_675_workspace_slot_bonus
Revises: e14_655_signup_allowlist_provider

NOTE: Revision ID is 27 chars — within VARCHAR(32) on alembic_version.
"""

from alembic import op

revision = "e15_675_workspace_slot_bonus"
down_revision = "e14_655_signup_allowlist_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add workspace_slot_bonus to users, grandfather existing owners."""
    # 1. Add column idempotently. INTEGER NOT NULL DEFAULT 0 is metadata-only
    #    in PG >= 11 (no table rewrite).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'users'
                  AND column_name = 'workspace_slot_bonus'
            ) THEN
                ALTER TABLE users
                  ADD COLUMN workspace_slot_bonus INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$;
        """
    )

    # 2. Acquire an exclusive lock on users for the rest of this transaction.
    #    Prevents a concurrent INSERT INTO workspaces from changing
    #    ``owned_count`` between the sub-SELECT and the UPDATE write.
    op.execute("LOCK TABLE users IN EXCLUSIVE MODE")

    # 3. Grandfather backfill. The sub-SELECT mirrors plan_resolver's
    #    runtime predicate exactly:
    #      - workspaces.owner_user_id = users.user_id  (OAuth sub VARCHAR join)
    #      - workspaces.deleted_at IS NULL              (soft-delete filter)
    #    GREATEST(0, ...) is defensive (COUNT(*) can never be negative,
    #    but keeps intent explicit if the subquery is later edited).
    #    No outer WHERE on users — the table has no deleted_at column.
    op.execute(
        """
        UPDATE users
        SET workspace_slot_bonus = GREATEST(
            0,
            (
                SELECT COUNT(*)
                FROM workspaces
                WHERE workspaces.owner_user_id = users.user_id
                  AND workspaces.deleted_at IS NULL
            ) - 1
        )
        WHERE (
            SELECT COUNT(*)
            FROM workspaces
            WHERE workspaces.owner_user_id = users.user_id
              AND workspaces.deleted_at IS NULL
        ) > 1
        """
    )


def downgrade() -> None:
    """Drop the workspace_slot_bonus column.

    Bonus values are not preserved — they are recomputable from live
    workspace state on re-upgrade.
    """
    op.drop_column("users", "workspace_slot_bonus")
