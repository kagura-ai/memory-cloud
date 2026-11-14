"""Clear allowlist adders erased before v0.162.0 (#2817).

``signup_allowlist.added_by_user_id`` names who added a row (the admin of a
manual add, the inviter of a redeemed invite link). It has no foreign key.
From v0.162.0 erasing or deleting an account clears it from the column in the
transaction that removes the account (#2814). A row whose adder was removed
before that still holds the person's user id, and no later removal can clear
it: no ``users`` row with that id is left.

This one-off cleanup sets the column to NULL on every row whose value matches
no ``users.user_id`` and does not start with ``operator:`` (in
``SYSTEM_ADMIN_MODE=service`` the operator principal adds rows and has no
``users`` row). No row is deleted: each still admits the person it names.
``updated_at`` is left alone (one raw set-based UPDATE). The log carries the
number of rows changed and nothing else.

Downgrade does nothing: the cleared ids are not kept anywhere. Re-running the
upgrade is safe; it clears only what is orphaned at that time.

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import logging
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f49_2817_clear_allowlist_adders"
down_revision: str | None = "f48_2805_public_usage_budgets"
branch_labels: str | None = None
depends_on: str | None = None

log = logging.getLogger("alembic.runtime.migration")

# The service-mode operator principal's prefix, written out because a revision
# stays frozen (auth.system_admin_mode.OPERATOR_PRINCIPAL_PREFIX; the migration
# test pins the two equal).
OPERATOR_PREFIX = "operator:"

CLEAR_ORPHANED_ADDERS = (
    "UPDATE signup_allowlist AS a "
    "SET added_by_user_id = NULL "
    "WHERE a.added_by_user_id IS NOT NULL "
    "AND a.added_by_user_id NOT LIKE 'operator:%' "
    "AND NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = a.added_by_user_id)"
)


def upgrade() -> None:
    """Set orphaned non-operator adders to NULL; keep every row."""
    result = cast(CursorResult[Any], op.get_bind().execute(sa.text(CLEAR_ORPHANED_ADDERS)))
    # The count only: no user id, address or label.
    log.info("f49_2817 cleared allowlist adders: %d", int(result.rowcount or 0))


def downgrade() -> None:
    """Nothing to restore: the cleared ids are not kept anywhere."""
