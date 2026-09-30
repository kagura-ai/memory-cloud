"""#1752: back-fill ``users.email_verified_at`` for OAuth accounts created
since the #1678 back-fill.

Security-change notices go only to a verified address. Migration
``e86_1678_email_password`` marked every account that had a
``user_oauth_providers`` row as verified, but accounts created by an OAuth
sign-in after it ran were stored with ``email_verified_at`` NULL (sign-in did
not set it until #1752). Without this they would get no notice until their
next OAuth sign-in. The same rule is applied again: an account with a linked
provider whose address is still unverified gets ``now``; ``@local`` addresses
are never marked.

From #1752 on, sign-in sets the column itself, and only when the provider
attests the address as verified.

Downgrade is a no-op: the rows cannot be told apart from those verified by a
sign-in or an emailed link.

Revision ID: e88_1752_verified_backfill
Revises: e87_1738_purge_indexes

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e88_1752_verified_backfill"
down_revision: str | None = "e87_1738_purge_indexes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE users SET email_verified_at = (now() AT TIME ZONE 'UTC')"
            " WHERE email_verified_at IS NULL"
            " AND lower(email) NOT LIKE '%@local'"
            " AND EXISTS (SELECT 1 FROM user_oauth_providers p"
            " WHERE p.user_id = users.user_id)"
        )
    )


def downgrade() -> None:
    pass
