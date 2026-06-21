"""Add share_keys table — context-scoped read-only TTL share key (#1027).

Issue #1027: a shareable, read-only, time-limited credential bound to a single
context. Kept in its own table (not a branch of ``api_keys``) so the security
invariants are structural: every existing endpoint authenticates against
``api_keys`` and therefore rejects a share key outright (fail-closed
allow-list), while the dedicated share-recall surface is the only place a
share key is honored.

Composition of prior art: context scope (#629/#626), read scope (#649),
TTL/expiry (#889). ``expires_at`` is NOT NULL — a share key always expires.

Revision ID: e43_1027_share_keys
Revises: e42_1048_reinforce_ranking
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e43_1027_share_keys"
down_revision = "e42_1048_reinforce_ranking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "share_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("context_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False, server_default="memory:read"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        # expires_at is NOT NULL — share keys are time-limited by construction.
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["context_id"], ["contexts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_share_keys_key_hash", "share_keys", ["key_hash"], unique=True)
    op.create_index("ix_share_keys_user_id", "share_keys", ["user_id"], unique=False)
    op.create_index("idx_share_keys_user_name", "share_keys", ["user_id", "name"], unique=False)
    op.create_index("idx_share_keys_context_id", "share_keys", ["context_id"], unique=False)
    op.create_index("idx_share_keys_revoked", "share_keys", ["revoked_at"], unique=False)
    op.create_index("idx_share_keys_expires", "share_keys", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_share_keys_expires", table_name="share_keys")
    op.drop_index("idx_share_keys_revoked", table_name="share_keys")
    op.drop_index("idx_share_keys_context_id", table_name="share_keys")
    op.drop_index("idx_share_keys_user_name", table_name="share_keys")
    op.drop_index("ix_share_keys_user_id", table_name="share_keys")
    op.drop_index("ix_share_keys_key_hash", table_name="share_keys")
    op.drop_table("share_keys")
