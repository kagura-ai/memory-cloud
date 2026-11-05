"""Add UNIQUE(context_id, user_id) on context_members (#2639).

The model has always documented "(context_id, user_id) must be unique", but
only non-unique indexes existed. ``add_context_member`` checks for an existing
row and then inserts, so two concurrent adds of one user both inserted: the
member list showed the user twice, and the role update and the member removal
(``scalar_one_or_none``) failed on the pair. The same shape as e47 on
``workspace_members``.

Step 1 deletes existing duplicates, keeping the highest-privilege, then
oldest row per pair (role owner > editor > viewer, then ``created_at``, then
the lowest ``id``): the same rule as e47 on ``workspace_members``, so the
migration never removes a context's only owner row (the app never removes an
owner row either). Step 2 adds the constraint.

A plain ``ALTER TABLE ... ADD CONSTRAINT`` (no ``CREATE UNIQUE INDEX
CONCURRENTLY``) is deliberate: ``context_members`` is a small membership
table like ``workspace_members``, so the index build under ACCESS EXCLUSIVE
is short. The lock wait runs under the migration ``SET LOCAL lock_timeout``
(``db.migration_helpers``); a 55P03 rolls the whole revision back and is
rerun as docs/deployment.md ("Migration lock timeout") says. A UNIQUE
constraint has no ``NOT VALID`` form, so there is no separate VALIDATE step.

Blue-green: two windows while OLD code still serves traffic, both self-heal:

1. Old code can commit a duplicate between the dedupe and the ALTER. The
   ALTER then fails, the transaction rolls back (the dedupe with it), and the
   rerun dedupes again before it adds the constraint.
2. Once the constraint exists, old code losing the add race gets a raw
   IntegrityError (500) instead of the new code's 400; gone after cutover.

Downgrade drops the constraint only; deleted duplicates are not restored.

Revision ID: f22_2639_context_members_uq
Revises: f21_2476_deferred_point_deletes
"""

from alembic import op
from db.constraint_names import CONTEXT_MEMBERS_CONTEXT_USER_UNIQUE

# revision identifiers, used by Alembic.
revision: str = "f22_2639_context_members_uq"
down_revision: str | None = "f21_2476_deferred_point_deletes"
branch_labels: str | None = None
depends_on: str | None = None

_DEDUP_SQL = """
DELETE FROM context_members cm
USING (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY context_id, user_id
               ORDER BY
                   CASE role
                       WHEN 'owner' THEN 0
                       WHEN 'editor' THEN 1
                       ELSE 2
                   END,
                   created_at,
                   id
           ) AS rn
    FROM context_members
) ranked
WHERE cm.id = ranked.id AND ranked.rn > 1
"""


def upgrade() -> None:
    op.execute(_DEDUP_SQL)
    op.create_unique_constraint(
        CONTEXT_MEMBERS_CONTEXT_USER_UNIQUE,
        "context_members",
        ["context_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_constraint(CONTEXT_MEMBERS_CONTEXT_USER_UNIQUE, "context_members", type_="unique")
