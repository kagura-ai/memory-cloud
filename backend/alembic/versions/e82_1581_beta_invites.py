"""#1581: closed-beta invite links — ``beta_invites`` + allowlist ``source`` widening.

A signed-in user mints a one-time link; whoever opens it may pass the signup
gate once, within its TTL. Two changes:

1. ``beta_invites`` — one row per minted link. Only ``token_hash``
   (``sha256_hex`` of the token, the API-key pattern) is stored; the plaintext
   is returned once at creation and never rests anywhere. Status
   (active / redeemed / expired / revoked) is derived from the timestamps, not
   stored. ``inviter_user_id`` is ``ON DELETE CASCADE`` so account erasure takes
   the inviter's links with it; ``redeemed_allowlist_entry_id`` is
   ``ON DELETE SET NULL`` so an admin pruning the invitee's allowlist row leaves
   the invite redeemed (and still counted against the inviter's quota).

2. ``valid_signup_allowlist_source`` admits ``'beta_invite'`` — the row the
   gate writes at redemption, keyed on the invitee's immutable IdP identity.

Downgrade restores the two-value CHECK, and refuses to run while any allowlist
row still uses ``'beta_invite'`` — re-creating the narrower constraint would
fail anyway, and silently deleting admission records is worse than a loud stop.

Revision ID: e82_1581_beta_invites
Revises: e81_1569_analysis_lane_cols

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e82_1581_beta_invites"
down_revision: str | None = "e81_1569_analysis_lane_cols"
branch_labels: str | None = None
depends_on: str | None = None

# Mirrors ``models.signup_gate.SignupAllowlistEntry.__table_args__`` byte-for-byte
# (schema drift test).
CK_VALID_SOURCE = "source IN ('manual', 'github_sponsors', 'beta_invite')"
CK_VALID_SOURCE_PREV = "source IN ('manual', 'github_sponsors')"


def upgrade() -> None:
    """Create ``beta_invites`` and widen the allowlist ``source`` CHECK."""
    op.drop_constraint("valid_signup_allowlist_source", "signup_allowlist", type_="check")
    op.create_check_constraint("valid_signup_allowlist_source", "signup_allowlist", CK_VALID_SOURCE)

    op.create_table(
        "beta_invites",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("inviter_user_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(), nullable=True),
        sa.Column("redeemed_allowlist_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["inviter_user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["redeemed_allowlist_entry_id"], ["signup_allowlist.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("token_hash", name="uq_beta_invites_token_hash"),
    )
    # Covers the per-inviter list and the quota COUNT.
    op.create_index("ix_beta_invites_inviter_user_id", "beta_invites", ["inviter_user_id"])


def downgrade() -> None:
    """Drop ``beta_invites`` and re-narrow the CHECK — only with no beta_invite rows."""
    beta_rows = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM signup_allowlist WHERE source = 'beta_invite'"))
        .scalar_one()
    )
    if beta_rows:
        raise RuntimeError(
            f"Cannot downgrade e82_1581_beta_invites: {beta_rows} signup_allowlist row(s) "
            "have source='beta_invite'. Delete them (or re-source them to 'manual') first."
        )
    op.drop_index("ix_beta_invites_inviter_user_id", table_name="beta_invites")
    op.drop_table("beta_invites")

    op.drop_constraint("valid_signup_allowlist_source", "signup_allowlist", type_="check")
    op.create_check_constraint(
        "valid_signup_allowlist_source", "signup_allowlist", CK_VALID_SOURCE_PREV
    )
