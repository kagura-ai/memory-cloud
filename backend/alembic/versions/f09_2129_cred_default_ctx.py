"""#2129: a default context bound to the credential (user API keys, OAuth grants).

``api_keys.default_context_id``: the MCP default context of the key — a
context-scoped tool called without ``context_id`` runs in it, below
``?context=`` on the URL. Nullable, ``ON DELETE SET NULL``, indexed. Two
CHECKs keep it off the keys that already have a meaning for a context:
``ck_api_keys_default_not_bound`` (a public-bound key, #626) and
``ck_api_keys_default_not_agent`` (an agent-bound key keeps its binding
default, #2125).

``oauth_grant_settings``: one row per ``(user_id, client_id)`` — the
per-grant settings ``oauth_tokens`` (one row per issuance) cannot hold.
``default_context_id`` as above; the row goes with its client
(``ON DELETE CASCADE``). Naive-UTC timestamps like the other OAuth tables.

No data step. The downgrade drops the table, the CHECKs, the index and the
column.

Revision ID: f09_2129_cred_default_ctx
Revises: f08_2107_chunk_gap
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers
revision = "f09_2129_cred_default_ctx"
down_revision = "f08_2107_chunk_gap"
branch_labels = None
depends_on = None

_KEY_INDEX = "idx_api_keys_default_context_id"
_KEY_FK = "fk_api_keys_default_context_id"
_CK_NOT_BOUND = "ck_api_keys_default_not_bound"
_CK_NOT_AGENT = "ck_api_keys_default_not_agent"
_TABLE = "oauth_grant_settings"


def upgrade() -> None:
    """Add the key column, its index and CHECKs, and the grant settings table."""
    op.add_column(
        "api_keys",
        sa.Column("default_context_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        _KEY_FK,
        "api_keys",
        "contexts",
        ["default_context_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(_KEY_INDEX, "api_keys", ["default_context_id"])
    op.create_check_constraint(
        _CK_NOT_BOUND, "api_keys", "default_context_id IS NULL OR bound_context_id IS NULL"
    )
    op.create_check_constraint(
        _CK_NOT_AGENT, "api_keys", "default_context_id IS NULL OR agent_id IS NULL"
    )

    op.create_table(
        _TABLE,
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("client_id", sa.String(48), nullable=False),
        sa.Column("default_context_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["oauth_clients.client_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["default_context_id"], ["contexts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id", "client_id"),
    )
    op.create_index("idx_oauth_grant_settings_client_id", _TABLE, ["client_id"])
    op.create_index("idx_oauth_grant_settings_default_context_id", _TABLE, ["default_context_id"])


def downgrade() -> None:
    """Drop the table, then the key CHECKs, index, foreign key and column."""
    op.drop_index("idx_oauth_grant_settings_default_context_id", table_name=_TABLE)
    op.drop_index("idx_oauth_grant_settings_client_id", table_name=_TABLE)
    op.drop_table(_TABLE)
    op.drop_constraint(_CK_NOT_AGENT, "api_keys", type_="check")
    op.drop_constraint(_CK_NOT_BOUND, "api_keys", type_="check")
    op.drop_index(_KEY_INDEX, table_name="api_keys")
    op.drop_constraint(_KEY_FK, "api_keys", type_="foreignkey")
    op.drop_column("api_keys", "default_context_id")
