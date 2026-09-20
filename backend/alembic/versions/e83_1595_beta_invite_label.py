"""#1595: ``beta_invites.label`` — the inviter's own note on a link.

The plaintext URL is shown once and only its hash is stored (#1581), so after a
few invites nothing told the inviter which link went to whom. ``label`` is an
optional, inviter-private free-text note (a name, an address) set at creation
and carried over by a reissue.

NULLable ``VARCHAR(100)``, no default, no backfill — existing rows simply have
no label. It sits on ``beta_invites``, whose ``inviter_user_id`` is
``ON DELETE CASCADE``, so account erasure removes it with the inviter's links.

Downgrade drops the column (the labels are lost; the invites are untouched).

Revision ID: e83_1595_beta_invite_label
Revises: e82_1581_beta_invites

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e83_1595_beta_invite_label"
down_revision: str | None = "e82_1581_beta_invites"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the NULLable ``label`` column."""
    op.add_column("beta_invites", sa.Column("label", sa.String(length=100), nullable=True))


def downgrade() -> None:
    """Drop ``label``."""
    op.drop_column("beta_invites", "label")
