"""#2800: target a service notice by plan: ``mcp_notices.plans``.

- ``plans varchar(50)[]``, nullable, no default (the width of
  ``workspaces.plan_name``). ``NULL`` = every plan, so existing notices still
  go to everyone; no backfill. A nullable column without a default does not
  rewrite the table.
- CHECK ``ck_mcp_notices_plans_nonempty``: ``NULL`` or at least one entry.
  The names themselves are checked by the API against the tier registry, not
  here, so a new tier needs no change to ``mcp_notices``. Added with
  ``op.create_check_constraint`` (the form ``tests/test_schema_drift.py``
  parses), byte-identical to the model.

Downgrade guard: dropping the column would turn every live or scheduled
notice with plans into a notice for everyone who opted in. So the downgrade
first takes the create lock (the transaction-level advisory lock
``create_notice`` takes, so a targeted create that has inserted but not yet
committed is counted instead of dropped under), then counts the notices with
plans that are neither withdrawn nor expired, and refuses (``RuntimeError``)
while there is any: withdraw them first through
``POST /api/v1/admin/mcp-notices/<id>/withdraw``, which writes the audit row.
The lock is held until alembic's single migration transaction ends, bounded
by ``KAGURA_MIGRATION_LOCK_TIMEOUT`` (default 5 s); a timeout means rerun.
Otherwise the CHECK and the column are dropped; the plans of ended notices
then stay only in their create audit rows.

Not guarded, and not guardable here: rolling the APPLICATION back without
this downgrade (``deploy.sh --rollback`` flips the colour and runs no
downgrade). Code from before #2800 ignores the column and delivers a live or
scheduled targeted notice to everyone who opted in. Before such a rollback,
set ``MCP_NOTICES_ENABLED=false`` on the colour that comes back, or withdraw
those notices (docs/deployment.md, "Service notices over MCP").

Revision ID: f45_2800_mcp_notice_plans
Revises: f44_2802_sync_installations

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f45_2800_mcp_notice_plans"
down_revision: str | None = "f44_2802_sync_installations"
branch_labels: str | None = None
depends_on: str | None = None

# Mirrors ``models.mcp_notice.McpNotice.__table_args__`` byte-for-byte
# (schema drift test).
CK_PLANS_NONEMPTY = "plans IS NULL OR cardinality(plans) >= 1"

# The create lock of ``services.mcp_notice_service._CREATE_LOCK_SQL``, copied
# (a migration imports nothing from ``src``); pinned equal by
# tests/services/test_mcp_notice_service.py.
CREATE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('mcp_notices_create', 0))"

_LIVE_TARGETED_SQL = (
    "SELECT count(*) FROM mcp_notices"
    " WHERE plans IS NOT NULL AND withdrawn_at IS NULL AND expires_at > now()"
)


def upgrade() -> None:
    """Add the nullable ``plans`` column and its non-empty CHECK."""
    op.add_column(
        "mcp_notices",
        sa.Column("plans", postgresql.ARRAY(sa.String(length=50)), nullable=True),
    )
    op.create_check_constraint("ck_mcp_notices_plans_nonempty", "mcp_notices", CK_PLANS_NONEMPTY)


def downgrade() -> None:
    """Drop the CHECK and the column; refuse while a targeted notice is live or scheduled."""
    bind = op.get_bind()
    bind.execute(sa.text(CREATE_LOCK_SQL))
    live = int(bind.execute(sa.text(_LIVE_TARGETED_SQL)).scalar_one())
    if live > 0:
        raise RuntimeError(
            f"{live} live or scheduled service notice(s) are targeted by plan; downgrading "
            "f45_2800_mcp_notice_plans would deliver them to everyone who opted in. Withdraw "
            "them first through POST /api/v1/admin/mcp-notices/<id>/withdraw, then rerun."
        )
    op.drop_constraint("ck_mcp_notices_plans_nonempty", "mcp_notices", type_="check")
    op.drop_column("mcp_notices", "plans")
