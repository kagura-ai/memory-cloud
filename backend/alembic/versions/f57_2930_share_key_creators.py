"""Delete share keys whose creating account does not exist (#2930).

Share keys belong to the account that created them: deleting or erasing an
account deletes them. This revision applies that rule to the rows already
stored. ``share_keys.user_id`` has no foreign key, so it deletes every row
whose ``user_id`` matches no ``users.user_id``, in one set-based statement.
A share key of an existing account is not touched. The log carries the
number of rows deleted and nothing else.

Locks: row locks on the deleted ``share_keys`` rows only (the ``users`` rows
are read, not locked); no table lock and no DDL. It is safe while the
previous release serves traffic: a request that carries a deleted key gets
the answer of an unknown key, and nothing else reads these rows. Offline
(``alembic upgrade --sql``) it emits the statement and logs no count.

Downgrade does nothing: the deleted rows are not kept anywhere. Re-running the
upgrade is safe; it deletes only what has no creator at that time.

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import logging

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f57_2930_share_key_creators"
down_revision: str | None = "f56_2918_workspace_slots"
branch_labels: str | None = None
depends_on: str | None = None

log = logging.getLogger("alembic.runtime.migration")

DELETE_SHARE_KEYS_WITHOUT_CREATOR = (
    "DELETE FROM share_keys AS s "
    "WHERE NOT EXISTS (SELECT 1 FROM users AS u WHERE u.user_id = s.user_id)"
)


def upgrade() -> None:
    """Delete the share keys whose creating account does not exist."""
    result = op.get_bind().execute(sa.text(DELETE_SHARE_KEYS_WITHOUT_CREATOR))
    # Offline (``--sql``) there is no result to count.
    rowcount = getattr(result, "rowcount", None)
    if rowcount is not None:
        # The count only: no key, name, context or account.
        log.info("f57_2930 deleted share keys: %d", int(rowcount))


def downgrade() -> None:
    """Nothing to restore: the deleted rows are not kept anywhere."""
