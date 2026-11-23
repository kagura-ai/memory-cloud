"""#2984: the keyword (BM25) field lengths of a memory's point, on ``memories``.

Adds five nullable INTEGER columns, no default: ``keyword_summary_len``,
``keyword_context_summary_len``, ``keyword_content_len``,
``keyword_reading_len`` (a summary point's four fields) and
``keyword_fulltext_len`` (a resource point's one field). The point writers
store the token counts they indexed; a context's averages over its live rows
weight the keyword leg (``services.keyword_stats``).

No backfill: every existing row reads NULL, which leaves it out of the
averages until its point is written again or
``python -m src.cli.rebuild_keyword_vectors --all --apply`` reaches it (see
docs/deployment.md). Adding a nullable column without a default is
metadata-only on PostgreSQL: no table rewrite, no scan.

Locks: the first ``ADD COLUMN`` takes an ``ACCESS EXCLUSIVE`` lock on
``memories``, held until the migration's transaction commits (five
metadata-only statements, milliseconds). It waits for, and then briefly
blocks, the transactions using the table; the migration ``lock_timeout``
bounds the wait. No other table is locked: no constraint, index or foreign
key is added.

Order: run this migration BEFORE starting the new code. The new code selects
the columns wherever it loads a memory row. The previous release never names
them (its INSERTs leave them NULL and its SELECTs list their columns), so it
keeps working against the migrated schema. Re-runnable (``IF NOT EXISTS``).

Downgrade drops the five columns; the rows stay and the keyword weighting of
the points already written stays as it was written.

Revision ID: f61_2984_keyword_lengths
Revises: f60_2979_domain_proof_via
"""

import sqlalchemy as sa

from alembic import op

revision: str = "f61_2984_keyword_lengths"
down_revision: str | None = "f60_2979_domain_proof_via"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "memories"
_COLUMNS = (
    "keyword_summary_len",
    "keyword_context_summary_len",
    "keyword_content_len",
    "keyword_reading_len",
    "keyword_fulltext_len",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, sa.Integer(), nullable=True), if_not_exists=True)


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column(_TABLE, name, if_exists=True)
