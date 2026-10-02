"""#1784: identity links — one person's accounts counted as one owner.

``identity_links`` groups ``users`` rows that belong to the same person (a
CLI admin and an OAuth account, say). Rows that share a ``group_id`` are one
link set; ``user_id`` is unique, so an account is in at most one set.
Ownership checks on private contexts and their memories match any member of
the caller's set. Nothing else is shared: roles and workspace membership stay
per account.

Rows cascade with the account. An existing deployment starts with no links;
nothing is linked by email (#481).

Downgrade drops the table: private contexts read as single-owner again.

Revision ID: e90_1784_identity_links
Revises: e89_1769_known_devices

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e90_1784_identity_links"
down_revision: str | None = "e89_1769_known_devices"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "identity_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            sa.String(255),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("linked_by", sa.String(255), nullable=False),
        sa.Column("linked_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", name="identity_links_user_id_key"),
    )
    op.create_index("ix_identity_links_group_id", "identity_links", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_identity_links_group_id", table_name="identity_links")
    op.drop_table("identity_links")
