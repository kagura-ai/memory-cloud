"""#1240: partial unique index — one 'running' analysis per (workspace, context).

``AnalysisOrchestrator.start()``'s idempotency guard is SELECT-then-INSERT:
two concurrent starts can both pass the SELECT before either flushes, creating
duplicate ``status='running'`` rows and bypassing the daily quota's read-only
COUNT. This index makes the DB enforce the invariant; the loser's INSERT
raises IntegrityError, which ``start()`` translates to the same 409
ConflictError the guard raises.

Pre-step: the bug this fixes also STRANDS rows at 'running' (malformed MCP
date params / ``_mark_failed`` on a poisoned session — both fixed in the same
PR). A production DB may therefore already hold duplicate running rows for
one (workspace, context), which would make CREATE UNIQUE INDEX fail. Mark all
but the NEWEST running row per pair as failed first — the older ones are by
definition abandoned (a genuinely live run would have refused to start while
they were running).

Blue-green safety: additive index + a data repair. Two residual windows
while OLD app code still serves traffic (both self-heal, neither corrupts):

1. Old code can commit a fresh duplicate 'running' pair between the repair
   UPDATE's snapshot and the index build — CREATE UNIQUE INDEX then fails
   and the migration transaction rolls back; RE-RUNNING the deploy repairs
   the new duplicate and succeeds.
2. Once the index exists, old code losing the start race surfaces a raw
   IntegrityError (500) instead of the new code's 409 translation — a
   correct rejection with a rougher status code, gone after cutover.

Revision ID: e61_1240_one_running_uq
Revises: e60_1228_read_attributions

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "e61_1240_one_running_uq"
down_revision = "e60_1228_read_attributions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Repair duplicate running rows, then create the partial unique index."""
    op.execute(
        sa.text(
            """
            UPDATE memory_analyses
            SET status = 'failed',
                error = 'Superseded duplicate running row, repaired by the '
                        '#1240 one-running-run migration.',
                finished_at = (now() AT TIME ZONE 'utc')
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY workspace_id, context_id
                               ORDER BY started_at DESC, id DESC
                           ) AS rn
                    FROM memory_analyses
                    WHERE status = 'running'
                ) ranked
                WHERE ranked.rn > 1
            )
            """
        )
    )
    op.create_index(
        "uq_memory_analyses_one_running",
        "memory_analyses",
        ["workspace_id", "context_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    """Drop the partial unique index (the data repair is not reverted)."""
    op.drop_index("uq_memory_analyses_one_running", table_name="memory_analyses")
