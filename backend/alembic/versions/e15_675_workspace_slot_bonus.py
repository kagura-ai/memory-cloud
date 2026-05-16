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

    # 2. Acquire exclusive locks on BOTH users and workspaces for the rest
    #    of this transaction. The users lock alone would not block a
    #    concurrent INSERT INTO workspaces — that could change the count
    #    we compute below. Locking both tables makes the grandfather
    #    computation a true atomic snapshot. SHARE ROW EXCLUSIVE on
    #    workspaces still allows concurrent reads but blocks writers.
    op.execute("LOCK TABLE users IN EXCLUSIVE MODE")
    op.execute("LOCK TABLE workspaces IN SHARE ROW EXCLUSIVE MODE")

    # 3. Grandfather backfill via a single CTE that computes COUNT(*)
    #    exactly once per user. The previous form had two correlated
    #    sub-SELECTs (outer WHERE + inner SET) which duplicated the
    #    predicate and theoretically allowed two snapshot reads to
    #    diverge inside one UPDATE. The CTE form is one read, one
    #    UPDATE, no duplication.
    #
    #    Predicate mirrors plan_resolver's runtime exactly:
    #      - workspaces.owner_user_id = users.user_id  (OAuth sub VARCHAR join)
    #      - workspaces.deleted_at IS NULL              (soft-delete filter)
    #    HAVING COUNT(*) > 1 filters out users at base cap (1 owned, no
    #    grandfather needed); GREATEST(0, ...) is defensive even though
    #    HAVING ensures cnt >= 2.
    #    No outer WHERE on users — the table has no deleted_at column.
    op.execute(
        """
        WITH owned_counts AS (
            SELECT owner_user_id, COUNT(*) AS cnt
            FROM workspaces
            WHERE deleted_at IS NULL
            GROUP BY owner_user_id
            HAVING COUNT(*) > 1
        )
        UPDATE users
        SET workspace_slot_bonus = GREATEST(0, owned_counts.cnt - 1)
        FROM owned_counts
        WHERE users.user_id = owned_counts.owner_user_id
        """
    )


def downgrade() -> None:
    """Drop the workspace_slot_bonus column.

    Bonus values are not preserved — they are recomputable from live
    workspace state on re-upgrade.
    """
    op.drop_column("users", "workspace_slot_bonus")
