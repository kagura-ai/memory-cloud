"""Add source/paid_by cost-grade dimensions to sleep_reports (#523).

Issue #523 extends the cost-grade reporting table introduced by #471 with
two billing-classification columns:

- ``source`` — distinguishes scheduler-driven sleep runs (``'sleep'``)
  from on-demand memory-broadlistening analysis runs (``'analysis'``).
- ``paid_by`` — separates platform-billed runs (``'platform'``) from
  workspace-BYOK runs (``'byok'``). v1 broadlistening writes
  ``paid_by='byok'`` because the workspace's external API key handles the
  cost; sleep runs continue to write ``paid_by='platform'``.

Both columns are ``NOT NULL`` with a ``server_default`` so existing rows
are immediately classified as scheduler-driven platform-billed runs — the
only mode that existed before this migration — with no data backfill
required. On PostgreSQL >= 11 (production ran ``postgres:15-alpine``
when this migration shipped; PG 18.4 since #1302)
adding a ``NOT NULL`` column with a constant ``DEFAULT`` is a metadata-
only catalog change; no table rewrite occurs even on a populated table.

DB-level CHECK constraints enforce the enum sets:

    source  IN ('sleep', 'analysis')
    paid_by IN ('platform', 'byok')

The constraints are added via raw ``op.execute(sa.text(...))`` calls of
``ALTER TABLE ... ADD CONSTRAINT ... NOT VALID`` followed by
``ALTER TABLE ... VALIDATE CONSTRAINT ...`` — the zero-downtime two-step
that mirrors the ``b03_396_neural_edges_ws_ctx_not_null`` precedent so
the validation scan runs under SHARE UPDATE EXCLUSIVE rather than
ACCESS EXCLUSIVE on populated tables.

A composite index ``idx_sleep_reports_workspace_source_started`` on
``(workspace_id, source, started_at DESC)`` supports the #472 cost
aggregation queries that filter by workspace + run type and order newest-
first; the explicit DESC matches the ORM declaration so EXPLAIN can use
the index without a separate sort step.

This migration unblocks both #472 (cost aggregation API surfacing the
new axes) and #495 (broadlistening pipeline emitting analysis cost rows
in the same persist transaction).

Revision ID: d05_523_source_paid_by
Revises: d04_519_oauth_owner_nullable

NOTE: Revision IDs are capped at 32 chars because
``alembic_version.version_num`` is ``VARCHAR(32)`` in this database
(asyncpg raises ``StringDataRightTruncationError`` otherwise). This
migration uses ``d05_523_source_paid_by`` (22 chars) — well within the
cap. The Python filename is allowed to be longer than the revision id,
so the descriptive ``d05_523_costgrade_source_paid_by.py`` is kept for
grep-ability.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d05_523_source_paid_by"
down_revision = "d04_519_oauth_owner_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add source, paid_by columns + composite index to sleep_reports."""
    # 1. Columns (NOT NULL with server_default — populates existing rows).
    op.add_column(
        "sleep_reports",
        sa.Column(
            "source",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'sleep'"),
        ),
    )
    op.add_column(
        "sleep_reports",
        sa.Column(
            "paid_by",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'platform'"),
        ),
    )

    # 2. CHECK constraints — zero-downtime two-step (matches `b03_396`
    # precedent). ADD CONSTRAINT NOT VALID skips the synchronous full-table
    # scan and only checks new writes; VALIDATE CONSTRAINT then proves
    # historic rows under SHARE UPDATE EXCLUSIVE (does not block
    # reads/writes). Existing rows already satisfy both constraints because
    # the server_default in step 1 wrote 'sleep' / 'platform'.
    op.execute(
        sa.text(
            "ALTER TABLE sleep_reports "
            "ADD CONSTRAINT valid_sleep_report_source "
            "CHECK (source IN ('sleep', 'analysis')) NOT VALID"
        )
    )
    op.execute(sa.text("ALTER TABLE sleep_reports VALIDATE CONSTRAINT valid_sleep_report_source"))
    op.execute(
        sa.text(
            "ALTER TABLE sleep_reports "
            "ADD CONSTRAINT valid_sleep_report_paid_by "
            "CHECK (paid_by IN ('platform', 'byok')) NOT VALID"
        )
    )
    op.execute(sa.text("ALTER TABLE sleep_reports VALIDATE CONSTRAINT valid_sleep_report_paid_by"))

    # 3. Composite index for #472 aggregation queries. ``sa.text`` is used
    # for the DESC sort because op.create_index does not accept ORM
    # ``sa.desc(...)`` expressions in its column list.
    op.create_index(
        "idx_sleep_reports_workspace_source_started",
        "sleep_reports",
        ["workspace_id", "source", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    """Drop composite index, CHECK constraints, and columns in reverse order.

    Explicit drops are used even though PostgreSQL would cascade them when
    the underlying columns are dropped — this matches the codebase's
    established downgrade pattern (see ``c02_471_cost_grade_schema``) and
    keeps the migration symmetric with ``upgrade``.
    """
    op.drop_index(
        "idx_sleep_reports_workspace_source_started",
        table_name="sleep_reports",
    )
    op.drop_constraint(
        "valid_sleep_report_paid_by",
        "sleep_reports",
        type_="check",
    )
    op.drop_constraint(
        "valid_sleep_report_source",
        "sleep_reports",
        type_="check",
    )
    op.drop_column("sleep_reports", "paid_by")
    op.drop_column("sleep_reports", "source")
