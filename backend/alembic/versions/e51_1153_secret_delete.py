"""Secret delete: add 'delete' to the secret_access_log action CheckConstraint (#1153).

Owner-only hard-delete of a secret (#1153) appends a ``delete`` entry to the
per-workspace tamper-evident audit chain *before* the secret row is removed.
``valid_secret_access_log_action`` (from e50) only permits
``register/approve/put/get/revoke``, so the new action needs the CHECK widened.

This is a pure constraint swap on ``secret_access_log`` — DDL, so the
``BEFORE UPDATE OR DELETE`` append-only row trigger (which only guards row DML)
does not block it. No existing row carries the new action, so revalidation of
the recreated constraint passes.

Revision ID: e51_1153_secret_delete
Revises: e50_1128_secret_store
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e51_1153_secret_delete"
down_revision = "e50_1128_secret_store"
branch_labels = None
depends_on = None

_CONSTRAINT = "valid_secret_access_log_action"
_TABLE = "secret_access_log"
_ACTIONS_NEW = "('register', 'approve', 'put', 'get', 'revoke', 'delete')"
_ACTIONS_OLD = "('register', 'approve', 'put', 'get', 'revoke')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, f"action IN {_ACTIONS_NEW}")


def downgrade() -> None:
    # Safe to narrow only if no 'delete' rows exist; a delete is append-only and
    # cannot be rewritten, so downgrading a DB that already recorded a deletion
    # would fail constraint revalidation by design (the audit trail is immutable).
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, f"action IN {_ACTIONS_OLD}")
