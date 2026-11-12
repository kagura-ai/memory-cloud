"""Email + password sign-up (#1734).

Adds:

- ``users.provider_auto_link_blocked_at`` (nullable): set when the owner
  removed a sign-in provider or a password reset removed one. While set, a
  later Google sign-in with the account's address is not linked to the
  account automatically.
- ``email`` in the ``signup_allowlist`` provider CHECK: a beta invite
  redeemed by email sign-up writes an allowlist row keyed on the new
  account's ``user_id``. The CHECK is replaced ``NOT VALID`` and validated in
  a transaction of its own (``validate_constraint``), so the steps before it
  are re-runnable.
- ``email_signup_requests``: one pending sign-up per row (normalized address,
  SHA-256 of the emailed token, the beta invite's hash, locale, time zone,
  expiry). Deleted when the sign-up completes, when a newer request for the
  same address replaces it, and by the hourly cleanup once expired.

Revision ID: f37_1734_email_signup
Revises: f36_2781_oidc_provider
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from db.migration_helpers import validate_constraint

revision = "f37_1734_email_signup"
down_revision = "f36_2781_oidc_provider"
branch_labels = None
depends_on = None

_TABLE = "email_signup_requests"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("provider_auto_link_blocked_at", sa.DateTime(), nullable=True),
        if_not_exists=True,
    )
    op.execute(
        "ALTER TABLE signup_allowlist DROP CONSTRAINT IF EXISTS valid_signup_allowlist_provider"
    )
    op.execute(
        "ALTER TABLE signup_allowlist ADD CONSTRAINT valid_signup_allowlist_provider "
        "CHECK (provider IN ('github', 'google', 'email')) NOT VALID"
    )
    validate_constraint("signup_allowlist", "valid_signup_allowlist_provider")

    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("beta_invite_token_hash", sa.CHAR(64), nullable=True),
        sa.Column("locale", sa.String(10), nullable=True),
        sa.Column("timezone", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("token_hash", name="email_signup_requests_token_hash_key"),
        if_not_exists=True,
    )
    op.create_index("ix_email_signup_requests_email", _TABLE, ["email"], if_not_exists=True)
    op.create_index(
        "ix_email_signup_requests_expires_at", _TABLE, ["expires_at"], if_not_exists=True
    )


def downgrade() -> None:
    """Refused while any allowlist row has ``provider='email'``, or while any
    account carries ``provider_auto_link_blocked_at``.

    ``provider='email'`` rows are beta invites redeemed by email sign-up.
    Account erasure keeps them (it only blanks their labels), so erasing the
    accounts does not clear the way. The two-provider CHECK cannot be restored
    while they exist, and deleting them (``DELETE FROM signup_allowlist WHERE
    provider = 'email'``) nulls ``beta_invites.redeemed_allowlist_entry_id``
    (``ON DELETE SET NULL``): the inviter's record of those redemptions is
    lost. Delete them only on purpose, accepting that loss; otherwise stay on
    this revision.

    ``provider_auto_link_blocked_at`` is the only record of an owner's choice
    (an explicit unlink, a reset that unlinked a provider, "This wasn't me").
    Dropping it and upgrading again would bring back automatic Google linking
    and the confirmation mails for those accounts. Clear the column on purpose
    (``UPDATE users SET provider_auto_link_blocked_at = NULL``), accepting that
    loss, before downgrading.

    Both checks run under ACCESS EXCLUSIVE locks on ``users`` and
    ``signup_allowlist`` (what the column drop and the CHECK swap take anyway),
    in the transaction that also drops the column and swaps the CHECK
    ``NOT VALID``: no row can arrive between a check and the step it guards,
    and a refusal rolls all of it back. Only the VALIDATE runs afterwards in a
    transaction of its own; a new row is already held to the restored CHECK.
    The steps before it are re-runnable (the column check is skipped once the
    column is gone).
    """
    bind = op.get_bind()
    op.execute("LOCK TABLE users IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE signup_allowlist IN ACCESS EXCLUSIVE MODE")
    in_use = bind.execute(
        sa.text("SELECT 1 FROM signup_allowlist WHERE provider = 'email' LIMIT 1")
    ).first()
    if in_use is not None:
        raise RuntimeError(
            "f37_1734_email_signup downgrade refused: signup_allowlist still holds rows "
            "with provider='email' (beta invites redeemed by email sign-up). Deleting "
            "them nulls beta_invites.redeemed_allowlist_entry_id; do it only on purpose "
            "(#1734)"
        )
    has_column = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'users' AND column_name = 'provider_auto_link_blocked_at'"
        )
    ).first()
    if has_column is not None:
        blocked = bind.execute(
            sa.text("SELECT 1 FROM users WHERE provider_auto_link_blocked_at IS NOT NULL LIMIT 1")
        ).first()
        if blocked is not None:
            raise RuntimeError(
                "f37_1734_email_signup downgrade refused: users.provider_auto_link_blocked_at "
                "is set on some accounts; dropping it loses those owners' choice to stop "
                "automatic provider linking. Clear it on purpose first (#1734)"
            )
    op.drop_table(_TABLE, if_exists=True)
    op.execute(
        "ALTER TABLE signup_allowlist DROP CONSTRAINT IF EXISTS valid_signup_allowlist_provider"
    )
    op.execute(
        "ALTER TABLE signup_allowlist ADD CONSTRAINT valid_signup_allowlist_provider "
        "CHECK (provider IN ('github', 'google')) NOT VALID"
    )
    op.drop_column("users", "provider_auto_link_blocked_at", if_exists=True)
    validate_constraint("signup_allowlist", "valid_signup_allowlist_provider")
