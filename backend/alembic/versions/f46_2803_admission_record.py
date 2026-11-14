"""#2803: record on ``users`` how each account was admitted.

Adds four columns, written once by the code that creates the row:

- ``admitted_via``: ``admin_cli`` / ``allowlist`` / ``beta_invite`` /
  ``first_user`` / ``open_registration`` / ``unknown`` /
  ``workspace_invitation``;
- ``admitted_beta_invite_id``: the invite a ``beta_invite`` admission spent
  (no foreign key, so it survives the invite row);
- ``admitted_invite_issuer``: ``admin`` or ``user``;
- ``admitted_by_user_id``: the inviter of a user-minted invite, a foreign key
  to ``users.user_id`` with ``ON DELETE SET NULL`` (deleting the inviter's
  account clears it, nothing else).

Steps, all in alembic's one transaction (``env.py`` sets ``lock_timeout``):

1. add the four columns, ``admitted_via`` nullable without a default. The
   first ALTER takes ACCESS EXCLUSIVE on ``users`` and holds it until commit,
   so every read of ``users`` waits for the backfill. On a ``lock_timeout``
   failure the whole upgrade rolls back; rerun it.
2. backfill existing rows from the evidence there is, with raw set-based
   UPDATEs (no ``updated_at`` change), each only where ``admitted_via`` is
   still NULL, in this precedence:

   1. ``admin_cli``: a ``local:`` row with a ``login_id``;
   2. an e-mail account's ``user.signed_up`` audit row (its ``admission``);
   3. ``first_user``: ``is_initial_admin`` on the lowest row;
   4. ``beta_invite``: a ``beta_invite`` allowlist row of the creating
      identity (any state), or a ``beta_invite.redeemed`` audit row;
   5. ``allowlist``: a manual allowlist row of the creating identity created
      no later than the account;
   6. ``workspace_invitation``: a ``signup_admitted`` audit row (#2785);
   7. ``unknown``: everything else.

   The creating identity is ``('email', user_id)`` for an e-mail account, or
   the ``user_oauth_providers`` row whose ``oauth_sub`` is the account's
   ``user_id``; linked identities never count. ``added_by_user_id`` and the
   audit rows' ``inviter_user_id`` are never read (both can hold an erased
   person's id).

   Then, for every ``beta_invite`` row, one live ``beta_invites`` row: the
   one whose ``redeemed_allowlist_entry_id`` is the creating identity's row,
   else the one a ``beta_invite.redeemed`` audit row names. Its id is
   recorded, the issuer is ``admin`` when the inviter's role is ``admin`` now
   and ``user`` otherwise, and the inviter only for ``user``. With no live
   invite the three stay NULL. The label is never read.
3. ``admitted_via`` gets ``server_default 'unknown'`` and NOT NULL (an app
   instance from before #2803 in a rolling deploy records ``unknown``);
4. the four CHECKs (the table lock is held and every row was just written,
   so they are validated in place);
5. the foreign key, then 6. the index on ``admitted_by_user_id``;
7. one log line with the count per kind, nothing else.

Operator timing: deploy before pruning manual allowlist rows at the end of
the closed beta. A beta-invite admission survives the pruning through its
audit row; a manual-allowlist admission does not.

Revision ID: f46_2803_admission_record
Revises: f45_2800_mcp_notice_plans

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import json
import logging
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f46_2803_admission_record"
down_revision: str | None = "f45_2800_mcp_notice_plans"
branch_labels: str | None = None
depends_on: str | None = None

log = logging.getLogger("alembic.runtime.migration")

# Byte-identical to models.auth.User.__table_args__ (tests/test_schema_drift.py).
_VALID_ADMITTED_VIA = (
    "admitted_via IN ('admin_cli', 'allowlist', 'beta_invite', 'first_user', "
    "'open_registration', 'unknown', 'workspace_invitation')"
)
_VALID_ADMITTED_INVITE_ISSUER = "admitted_invite_issuer IN ('admin', 'user')"
_ADMITTED_INVITE_FIELDS_BETA_ONLY = (
    "admitted_via = 'beta_invite' OR (admitted_beta_invite_id IS NULL AND "
    "admitted_invite_issuer IS NULL AND admitted_by_user_id IS NULL)"
)
_ADMITTED_BY_USER_ISSUER_USER = "admitted_by_user_id IS NULL OR admitted_invite_issuer = 'user'"

_KINDS = (
    "admin_cli",
    "allowlist",
    "beta_invite",
    "first_user",
    "open_registration",
    "unknown",
    "workspace_invitation",
)

_FK_NAME = "fk_users_admitted_by_user_id"
_INDEX_NAME = "ix_users_admitted_by_user_id"

# The allowlist row ``a`` belongs to the identity that created account ``u``.
_CREATING_IDENTITY = (
    "a.subject_id = u.user_id AND (a.provider = 'email' OR EXISTS ("
    "SELECT 1 FROM user_oauth_providers p WHERE p.user_id = u.user_id "
    "AND p.oauth_sub = u.user_id AND p.provider = a.provider))"
)

_STEPS: tuple[str, ...] = (
    (
        "UPDATE users u SET admitted_via = 'admin_cli' "
        "WHERE u.admitted_via IS NULL AND u.user_id LIKE 'local:%' AND u.login_id IS NOT NULL"
    ),
    (
        "UPDATE users u SET admitted_via = s.admission FROM ("
        "SELECT DISTINCT ON (l.user_id) l.user_id, l.user_metadata->>'admission' AS admission "
        "FROM audit_logs l WHERE l.action = 'user.signed_up' ORDER BY l.user_id, l.id"
        ") s WHERE u.admitted_via IS NULL AND s.user_id = u.user_id AND s.admission IN "
        "('first_user', 'open_registration', 'workspace_invitation', 'beta_invite')"
    ),
    (
        "UPDATE users u SET admitted_via = 'first_user' "
        "WHERE u.admitted_via IS NULL AND u.is_initial_admin "
        "AND NOT (u.user_id LIKE 'local:%' AND u.login_id IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM users o WHERE o.id < u.id)"
    ),
    (
        "UPDATE users u SET admitted_via = 'beta_invite' WHERE u.admitted_via IS NULL AND ("  # noqa: S608
        "EXISTS (SELECT 1 FROM signup_allowlist a WHERE a.source = 'beta_invite' AND "
        + _CREATING_IDENTITY
        + ") OR EXISTS (SELECT 1 FROM audit_logs l "
        "WHERE l.action = 'beta_invite.redeemed' AND l.user_id = u.user_id))"
    ),
    (
        "UPDATE users u SET admitted_via = 'allowlist' WHERE u.admitted_via IS NULL AND "  # noqa: S608
        "EXISTS (SELECT 1 FROM signup_allowlist a WHERE a.source <> 'beta_invite' "
        "AND a.provider <> 'email' AND a.created_at <= u.created_at AND " + _CREATING_IDENTITY + ")"
    ),
    (
        "UPDATE users u SET admitted_via = 'workspace_invitation' "
        "WHERE u.admitted_via IS NULL AND EXISTS (SELECT 1 FROM audit_logs l "
        "WHERE l.action = 'signup_admitted' AND l.user_id = u.user_id)"
    ),
    "UPDATE users u SET admitted_via = 'unknown' WHERE u.admitted_via IS NULL",
)

# One live invite per beta_invite account: through the creating identity's
# allowlist row first, else through a redemption audit row; the earliest
# redemption, then the id. Columns only, never the label.
_DETAILS = (
    "WITH candidates AS ("  # noqa: S608
    "SELECT u.id AS uid, 1 AS route, b.id AS invite_id, b.redeemed_at, "
    "b.inviter_user_id, inviter.role AS inviter_role "
    "FROM users u JOIN signup_allowlist a ON a.source = 'beta_invite' AND "
    + _CREATING_IDENTITY
    + " JOIN beta_invites b ON b.redeemed_allowlist_entry_id = a.id "
    "JOIN users inviter ON inviter.user_id = b.inviter_user_id "
    "WHERE u.admitted_via = 'beta_invite' AND u.admitted_beta_invite_id IS NULL "
    "UNION ALL "
    "SELECT u.id, 2, b.id, b.redeemed_at, b.inviter_user_id, inviter.role "
    "FROM users u JOIN audit_logs l ON l.action = 'beta_invite.redeemed' "
    "AND l.user_id = u.user_id "
    "JOIN beta_invites b ON l.resource = 'beta_invite:' || b.id::text "
    "JOIN users inviter ON inviter.user_id = b.inviter_user_id "
    "WHERE u.admitted_via = 'beta_invite' AND u.admitted_beta_invite_id IS NULL"
    "), pick AS ("
    "SELECT DISTINCT ON (uid) uid, invite_id, inviter_user_id, inviter_role FROM candidates "
    "ORDER BY uid, route, redeemed_at NULLS LAST, invite_id"
    ") UPDATE users u SET admitted_beta_invite_id = pick.invite_id, "
    "admitted_invite_issuer = CASE WHEN pick.inviter_role = 'admin' THEN 'admin' "
    "ELSE 'user' END, "
    "admitted_by_user_id = CASE WHEN pick.inviter_role = 'admin' THEN NULL "
    "ELSE pick.inviter_user_id END "
    "FROM pick WHERE pick.uid = u.id"
)


def _rowcount(sql: str) -> int:
    result = cast(CursorResult[Any], op.get_bind().execute(sa.text(sql)))
    return int(result.rowcount or 0)


def upgrade() -> None:
    """Add the columns, backfill them from the evidence, then constrain them."""
    op.add_column("users", sa.Column("admitted_via", sa.String(32), nullable=True))
    op.add_column(
        "users", sa.Column("admitted_beta_invite_id", sa.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("users", sa.Column("admitted_invite_issuer", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("admitted_by_user_id", sa.String(255), nullable=True))

    for sql in _STEPS:
        op.get_bind().execute(sa.text(sql))
    # Every kind, including those the signed_up step writes, counted once.
    counts: dict[str, int] = dict.fromkeys(_KINDS, 0)
    by_kind = op.get_bind().execute(
        sa.text("SELECT admitted_via, count(*) FROM users GROUP BY admitted_via")
    )
    counts.update({str(kind): int(n) for kind, n in by_kind})
    filled = _rowcount(_DETAILS)
    counts["beta_invite_details_filled"] = filled
    counts["beta_invite_details_missing"] = counts["beta_invite"] - filled

    op.alter_column(
        "users",
        "admitted_via",
        existing_type=sa.String(32),
        server_default=sa.text("'unknown'"),
        nullable=False,
    )
    op.create_check_constraint("valid_admitted_via", "users", _VALID_ADMITTED_VIA)
    op.create_check_constraint(
        "valid_admitted_invite_issuer", "users", _VALID_ADMITTED_INVITE_ISSUER
    )
    op.create_check_constraint(
        "admitted_invite_fields_beta_only", "users", _ADMITTED_INVITE_FIELDS_BETA_ONLY
    )
    op.create_check_constraint(
        "admitted_by_user_issuer_user", "users", _ADMITTED_BY_USER_ISSUER_USER
    )
    op.create_foreign_key(
        _FK_NAME,
        "users",
        "users",
        ["admitted_by_user_id"],
        ["user_id"],
        ondelete="SET NULL",
    )
    op.create_index(_INDEX_NAME, "users", ["admitted_by_user_id"])

    # Counts only: no user id, address, invite id or label.
    log.info("f46_2803 admission backfill counts: %s", json.dumps(counts, sort_keys=True))


def downgrade() -> None:
    """Drop the admission record.

    The records written at sign-up are lost for good: a later upgrade rebuilds
    only what the evidence still gives (``unknown`` for the rest).
    """
    op.drop_index(_INDEX_NAME, table_name="users")
    op.drop_constraint(_FK_NAME, "users", type_="foreignkey")
    op.drop_constraint("admitted_by_user_issuer_user", "users", type_="check")
    op.drop_constraint("admitted_invite_fields_beta_only", "users", type_="check")
    op.drop_constraint("valid_admitted_invite_issuer", "users", type_="check")
    op.drop_constraint("valid_admitted_via", "users", type_="check")
    op.drop_column("users", "admitted_by_user_id")
    op.drop_column("users", "admitted_invite_issuer")
    op.drop_column("users", "admitted_beta_invite_id")
    op.drop_column("users", "admitted_via")
