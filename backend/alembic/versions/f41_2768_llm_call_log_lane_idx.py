"""#2768: index the platform LLM lanes' rows of ``llm_call_log``.

``idx_llm_call_log_platform_lane``: a btree on ``(caller, provider, model,
occurred_at)`` of the platform-paid rows (``WHERE paid_by = 'platform'``).
The operations status route reads, per ledgered lane, the latest successful
call of the last 30 days and the calls of the last hour and day. With only
``(occurred_at)`` and ``(workspace_id, occurred_at)`` to go on, a lane with
no recent call (an opt-in selector nobody uses next to a busy embedding
lane) read every ledger row of those 30 days on every status read. Partial:
calls paid with a workspace's own key are never read by those lookups.

Built CONCURRENTLY in an autocommit block (the f17 form): writes continue
during the scan. Only the read-only INVALID probe runs before the block, so
the upgrade can simply be run again after an interrupted build; an INVALID
leftover index is dropped before the ``IF NOT EXISTS`` create. It does not
run offline (``--sql``): the INVALID-index probe needs a live connection.

Revision ID: f41_2768_llm_call_log_lane_idx
Revises: f40_565_file_scan
"""

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import index_is_invalid

# revision identifiers, used by Alembic.
revision: str = "f41_2768_llm_call_log_lane_idx"
down_revision: str | None = "f40_565_file_scan"
branch_labels: str | None = None
depends_on: str | None = None

_INDEX_NAME = "idx_llm_call_log_platform_lane"
# Literal DDL (identifiers cannot be bound parameters).
_INDEX_DDL = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_llm_call_log_platform_lane "
    "ON llm_call_log (caller, provider, model, occurred_at) WHERE paid_by = 'platform'"
)
_DROP_DDL = "DROP INDEX CONCURRENTLY IF EXISTS idx_llm_call_log_platform_lane"


def upgrade() -> None:
    """Build the lane index without a write lock on ``llm_call_log``."""
    # CONCURRENTLY cannot run inside a transaction → the autocommit block.
    invalid = index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(_DROP_DDL))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the lane index, again without a write lock."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(_DROP_DDL))
