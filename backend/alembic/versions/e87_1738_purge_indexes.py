"""#1738: indexes for the password-reset grant purge and the link cleanup.

- ``ix_email_action_tokens_used_at`` / ``ix_email_action_tokens_expires_at``:
  the hourly cleanup deletes rows with ``used_at < cutoff OR expires_at <
  cutoff``; two single-column indexes let PostgreSQL bitmap-OR the condition
  instead of scanning the table.
- ``ix_oauth_device_codes_user_id``: a password reset deletes the account's
  device codes by ``user_id``.

Downgrade drops the three indexes.

Revision ID: e87_1738_purge_indexes
Revises: e86_1678_email_password

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e87_1738_purge_indexes"
down_revision: str | None = "e86_1678_email_password"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index("ix_email_action_tokens_used_at", "email_action_tokens", ["used_at"])
    op.create_index("ix_email_action_tokens_expires_at", "email_action_tokens", ["expires_at"])
    op.create_index("ix_oauth_device_codes_user_id", "oauth_device_codes", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_oauth_device_codes_user_id", table_name="oauth_device_codes")
    op.drop_index("ix_email_action_tokens_expires_at", table_name="email_action_tokens")
    op.drop_index("ix_email_action_tokens_used_at", table_name="email_action_tokens")
