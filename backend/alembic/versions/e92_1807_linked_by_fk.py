"""#1807: ``identity_links.linked_by`` becomes a nullable foreign key.

``linked_by`` names the account whose session created a link row. It was a
plain string, so deleting that account by a path that skips the hand-over
step (the ``delete_admin`` CLI, a direct row delete) left a deleted id on the
surviving rows. It now references ``users.user_id`` with ``ON DELETE SET
NULL``: the row stays, and NULL reads as "the account that made this link is
gone".

Steps:

1. drop NOT NULL;
2. set ``linked_by`` to NULL on every row whose value does not name an
   account of the row's own set: the dangling ids of deleted accounts, which
   would otherwise make step 3 fail, and the ids of accounts that were
   unlinked from the set (before #1807 an unlink never rewrote them);
3. add the foreign key.

Downgrade drops the key, fills NULLs with the row's own ``user_id`` (what the
hand-over step writes) and restores NOT NULL.

Revision ID: e92_1807_linked_by_fk
Revises: e91_1008_public_ids

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e92_1807_linked_by_fk"
down_revision: str | None = "e91_1008_public_ids"
branch_labels: str | None = None
depends_on: str | None = None

_FK_NAME = "identity_links_linked_by_fkey"


def upgrade() -> None:
    op.alter_column("identity_links", "linked_by", existing_type=sa.String(255), nullable=True)
    op.execute(
        sa.text(
            "UPDATE identity_links AS row SET linked_by = NULL "
            "WHERE row.linked_by IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM identity_links AS member "
            "WHERE member.user_id = row.linked_by AND member.group_id = row.group_id)"
        )
    )
    op.create_foreign_key(
        _FK_NAME,
        "identity_links",
        "users",
        ["linked_by"],
        ["user_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, "identity_links", type_="foreignkey")
    op.execute(sa.text("UPDATE identity_links SET linked_by = user_id WHERE linked_by IS NULL"))
    op.alter_column("identity_links", "linked_by", existing_type=sa.String(255), nullable=False)
