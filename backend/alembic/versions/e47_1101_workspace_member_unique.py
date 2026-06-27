"""Add UNIQUE(workspace_id, user_id) on workspace_members (#1101).

The model has always documented "(workspace_id, user_id) must be unique" and
every membership path does a check-then-insert, but the constraint was never
enforced at the DB level — only non-unique indexes existed. The break-glass
force-transfer ADD path (#1101) inserts a fresh OWNER member for a non-member
target while holding only the workspace row lock, which does NOT serialize
against the lock-free ``add_member`` / invitation-accept inserts. Without a DB
constraint that race could produce two membership rows for the same
(workspace_id, user_id). This makes the long-assumed invariant structural.

The upgrade first defensively de-duplicates any pre-existing rows (the app has
always assumed uniqueness, so this is expected to be a no-op), keeping the
highest-privilege then oldest row per (workspace_id, user_id), before creating
the unique constraint.

Revision ID: e47_1101_member_unique
Revises: e46_1095_entitlement_src
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e47_1101_member_unique"
down_revision = "e46_1095_entitlement_src"
branch_labels = None
depends_on = None

_DEDUP_SQL = """
DELETE FROM workspace_members wm
USING (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY workspace_id, user_id
               ORDER BY
                   CASE role
                       WHEN 'owner' THEN 0
                       WHEN 'admin' THEN 1
                       WHEN 'member' THEN 2
                       ELSE 3
                   END,
                   joined_at NULLS LAST,
                   id
           ) AS rn
    FROM workspace_members
) ranked
WHERE wm.id = ranked.id AND ranked.rn > 1
"""


def upgrade() -> None:
    op.execute(_DEDUP_SQL)
    op.create_unique_constraint(
        "uq_workspace_members_workspace_user",
        "workspace_members",
        ["workspace_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_workspace_members_workspace_user", "workspace_members", type_="unique")
