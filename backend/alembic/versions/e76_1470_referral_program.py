"""Referral program: ledger table + referral bonus column + user referral code.

Issue #1470.

Three additive changes, no backfill:

1. ``workspaces.referral_memory_bonus`` — a memory-quota bonus earned through
   the referral program, stacked into ``Workspace.effective_memory_limit``.

   This column is deliberately NOT named ``addon_*`` and is deliberately absent
   from ``internal_billing._ADDON_COLUMNS``, ``admin_plans._ADDON_FIELD_SPECS``
   and ``AddonCalculatorService``'s bonuses dict. Each of those paths would
   destroy it: the billing push zeroes every ``_ADDON_COLUMNS`` entry whenever
   it carries ``addons`` (full-replace), and the addon cache is rewritten with
   ``SUM(WorkspaceAddon.quantity * unit_value)`` on every recalc — the same
   class of bug #665 fixed for admin grants. Its source of truth is the
   ``referral_grants`` ledger below.

2. ``users.referral_code`` — nullable UNIQUE, lazily minted on first read.
   Never ``users.user_id``: that holds the OAuth ``sub`` claim used by sessions,
   API keys and MCP auth, and must not appear in a shareable URL.

3. ``referral_grants`` — the payout ledger. ``uq_referral_grants_referred_user``
   is the idempotency key (a user can be referred at most once, ever), and all
   four FKs are ``ON DELETE SET NULL`` so account erasure needs no new entry in
   ``account_erasure_service``'s explicit table sweep and so erasing an invitee
   does not retroactively void the referrer's earned quota.

Revision ID: e76_1470_referral_program
Revises: e75_1403_supersede_candidate
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "e76_1470_referral_program"
down_revision = "e75_1403_supersede_candidate"
branch_labels = None
depends_on = None

# Kept byte-identical to the literals in ``backend/src/models/referral.py`` and
# ``models/auth.py`` — ``backend/tests/test_schema_drift.py`` compares them.
CK_NOT_SELF = (
    "referrer_user_id IS NULL OR referred_user_id IS NULL OR referrer_user_id <> referred_user_id"
)
CK_BONUS_NONNEG = "referrer_bonus_memories >= 0 AND referred_bonus_memories >= 0"
CK_REFERRAL_BONUS_NONNEG = "referral_memory_bonus >= 0"


def upgrade() -> None:
    """Add the referral bonus column, the user referral code, and the ledger."""
    op.add_column(
        "workspaces",
        sa.Column(
            "referral_memory_bonus",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "referral_memory_bonus_nonneg",
        "workspaces",
        CK_REFERRAL_BONUS_NONNEG,
    )

    op.add_column(
        "users",
        sa.Column("referral_code", sa.String(length=24), nullable=True),
    )
    op.create_index(
        "ix_users_referral_code",
        "users",
        ["referral_code"],
        unique=True,
    )

    op.create_table(
        "referral_grants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("referrer_user_id", sa.String(length=255), nullable=True),
        sa.Column("referrer_workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("referred_user_id", sa.String(length=255), nullable=True),
        sa.Column("referred_workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("referrer_bonus_memories", sa.Integer(), nullable=False),
        sa.Column("referred_bonus_memories", sa.Integer(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["referrer_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["referred_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["referrer_workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["referred_workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("referred_user_id", name="uq_referral_grants_referred_user"),
        sa.CheckConstraint(CK_NOT_SELF, name="ck_referral_grants_not_self"),
        sa.CheckConstraint(CK_BONUS_NONNEG, name="ck_referral_grants_bonus_nonneg"),
    )
    op.create_index(
        "ix_referral_grants_referrer_user_id",
        "referral_grants",
        ["referrer_user_id"],
    )
    op.create_index(
        "ix_referral_grants_referrer_workspace_id",
        "referral_grants",
        ["referrer_workspace_id"],
    )
    op.create_index(
        "ix_referral_grants_referred_workspace_id",
        "referral_grants",
        ["referred_workspace_id"],
    )
    op.create_index(
        "ix_referral_grants_granted_at",
        "referral_grants",
        ["granted_at"],
    )
    # Covers the per-referrer cap COUNT, which always filters on
    # ``revoked_at IS NULL``.
    op.create_index(
        "ix_referral_grants_referrer_active",
        "referral_grants",
        ["referrer_user_id", "revoked_at"],
    )


def downgrade() -> None:
    """Drop the ledger, the referral code, and the bonus column."""
    op.drop_index("ix_referral_grants_referrer_active", table_name="referral_grants")
    op.drop_index("ix_referral_grants_granted_at", table_name="referral_grants")
    op.drop_index("ix_referral_grants_referred_workspace_id", table_name="referral_grants")
    op.drop_index("ix_referral_grants_referrer_workspace_id", table_name="referral_grants")
    op.drop_index("ix_referral_grants_referrer_user_id", table_name="referral_grants")
    op.drop_table("referral_grants")

    op.drop_index("ix_users_referral_code", table_name="users")
    op.drop_column("users", "referral_code")

    op.drop_constraint("referral_memory_bonus_nonneg", "workspaces", type_="check")
    op.drop_column("workspaces", "referral_memory_bonus")
