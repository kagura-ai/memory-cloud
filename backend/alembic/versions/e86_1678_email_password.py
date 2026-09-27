"""#1678: email + password sign-in — ``users.email_verified_at`` and
``email_action_tokens``.

- ``users.email_verified_at`` (naive UTC, nullable): when ownership of the
  account email was proven. Password sign-in by email requires it.
  Backfill: users with at least one ``user_oauth_providers`` row get ``now``
  (their address came from an IdP that verified it — Google's verified email,
  GitHub's primary + verified email). Emails ending in ``@local`` (CLI admins)
  are never marked.
- ``email_action_tokens``: single-use, expiring links (verify email / set a
  password / reset a password). Only the SHA-256 hex digest is stored
  (``token_hash`` CHAR(64), unique). ``purpose`` is CHECK-constrained.
  ``user_id`` is ``ON DELETE CASCADE``. ``(user_id, purpose)`` is indexed for
  the "invalidate outstanding tokens" update.

``users.auth_method`` and its CHECK are unchanged.

Downgrade drops the table and the column (verification timestamps and
outstanding links are lost).

Revision ID: e86_1678_email_password
Revises: e85_1665_terms_acceptances

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e86_1678_email_password"
down_revision: str | None = "e85_1665_terms_acceptances"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``users.email_verified_at`` (backfilled) and ``email_action_tokens``."""
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE users SET email_verified_at = (now() AT TIME ZONE 'UTC')"
            " WHERE email_verified_at IS NULL"
            " AND lower(email) NOT LIKE '%@local'"
            " AND EXISTS (SELECT 1 FROM user_oauth_providers p"
            " WHERE p.user_id = users.user_id)"
        )
    )

    op.create_table(
        "email_action_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_email_action_tokens_user_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="email_action_tokens_token_hash_key"),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'set_password', 'reset_password')",
            name="valid_email_action_purpose",
        ),
    )
    op.create_index(
        "ix_email_action_tokens_user_purpose",
        "email_action_tokens",
        ["user_id", "purpose"],
    )


def downgrade() -> None:
    """Drop ``email_action_tokens`` and ``users.email_verified_at``."""
    op.drop_index("ix_email_action_tokens_user_purpose", table_name="email_action_tokens")
    op.drop_table("email_action_tokens")
    op.drop_column("users", "email_verified_at")
