"""#3035: route and caller on public usage events; daily visitors; a counts index.

Adds two nullable columns to ``public_mcp_usage_events``: ``caller``
(VARCHAR(16), CHECK ``valid_pmue_caller``: ``anonymous``, ``bound_key``) and
``route`` (VARCHAR(8), CHECK ``valid_pmue_route``: ``mcp``, ``rest``). Each
CHECK is added ``NOT VALID`` and validated in a transaction of its own. NULL
is a gap row or a row written before this revision (all public MCP calls).

Creates ``public_mcp_daily_visitors`` (endpoint, UTC day, visitor key, the
workspace), primary key ``(endpoint_id, day, visitor_key)``, the key-shape
CHECK ``pmdv_visitor_key_shape`` and the purge index ``ix_pmdv_day``. No
foreign key.

Last, builds ``ix_pmue_endpoint_counts`` ``CONCURRENTLY``: the owner's day
counts read every column they name from it. An index left INVALID by an
interrupted build is dropped and built again on the next run.

No backfill: every existing row reads NULL (metadata-only on PostgreSQL).

Locks: the two ``ADD COLUMN`` (nullable, no default: catalog only) and the
``DROP / ADD CONSTRAINT ... NOT VALID`` take a brief ``ACCESS EXCLUSIVE`` lock
on ``public_mcp_usage_events`` under the migration ``lock_timeout``; the
first commit point releases it. Each VALIDATE scan then holds ``SHARE UPDATE
EXCLUSIVE`` (inserts continue). The concurrent index build holds ``SHARE
UPDATE EXCLUSIVE`` and waits for older transactions without blocking reads
or inserts; it runs outside the migration ``lock_timeout`` on purpose, and
its time grows with the events table. The new table has no foreign key. No
lock is taken on ``users``, ``workspaces``, ``contexts`` or any other table.

Order: run this migration BEFORE starting the new code. Both columns are
mapped, so the new writer's INSERT and the export reader name them. The
previous release never names them (its INSERT leaves them NULL) and does not
know the new table, so it keeps working against the migrated schema.
Re-runnable (``IF NOT EXISTS`` / ``DROP CONSTRAINT IF EXISTS``).

Downgrade drops the index concurrently, deletes the REST rows (the previous
schema cannot tell them from public MCP calls) in batches of 5,000 by id, each
batch committed on its own (a primary-key range walk, linear in the table;
inserts continue), then drops the visitor table, both CHECKs and both
columns.

Revision ID: f67_3035_public_visitors
Revises: f66_3019_sleep_coverage
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from db.migration_helpers import index_is_invalid, validate_constraint

revision: str = "f67_3035_public_visitors"
down_revision: str | None = "f66_3019_sleep_coverage"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "public_mcp_usage_events"
_VISITORS = "public_mcp_daily_visitors"
_COUNTS_INDEX = "ix_pmue_endpoint_counts"
_DOWNGRADE_BATCH = 5_000

_CREATE_COUNTS_INDEX = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pmue_endpoint_counts "
    "ON public_mcp_usage_events (endpoint_id, occurred_at) "
    "INCLUDE (workspace_id, kind, tool, outcome, route, caller) "
    "WHERE endpoint_id IS NOT NULL"
)
_DROP_COUNTS_INDEX = "DROP INDEX CONCURRENTLY IF EXISTS ix_pmue_endpoint_counts"
_DELETE_REST_BATCH = (
    "DELETE FROM public_mcp_usage_events WHERE id IN ("
    "SELECT id FROM public_mcp_usage_events WHERE route = 'rest' AND id > :after "
    "ORDER BY id LIMIT :limit) RETURNING id"
)


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("caller", sa.String(16), nullable=True), if_not_exists=True)
    op.add_column(_TABLE, sa.Column("route", sa.String(8), nullable=True), if_not_exists=True)
    op.execute("ALTER TABLE public_mcp_usage_events DROP CONSTRAINT IF EXISTS valid_pmue_caller")
    op.execute(
        "ALTER TABLE public_mcp_usage_events ADD CONSTRAINT valid_pmue_caller "
        "CHECK (caller IS NULL OR caller IN ('anonymous', 'bound_key')) NOT VALID"
    )
    op.execute("ALTER TABLE public_mcp_usage_events DROP CONSTRAINT IF EXISTS valid_pmue_route")
    op.execute(
        "ALTER TABLE public_mcp_usage_events ADD CONSTRAINT valid_pmue_route "
        "CHECK (route IS NULL OR route IN ('mcp', 'rest')) NOT VALID"
    )
    validate_constraint(_TABLE, "valid_pmue_caller")
    validate_constraint(_TABLE, "valid_pmue_route")

    op.create_table(
        _VISITORS,
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("visitor_key", sa.CHAR(32), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("endpoint_id", "day", "visitor_key"),
        sa.CheckConstraint("visitor_key ~ '^[0-9a-f]{32}$'", name="pmdv_visitor_key_shape"),
        if_not_exists=True,
    )
    op.create_index("ix_pmdv_day", _VISITORS, ["day"], if_not_exists=True)

    invalid = index_is_invalid(_COUNTS_INDEX)
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(_DROP_COUNTS_INDEX)
        op.execute(_CREATE_COUNTS_INDEX)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(_DROP_COUNTS_INDEX)
    bind = op.get_bind()
    has_route = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'public_mcp_usage_events' AND column_name = 'route'"
        )
    ).first()
    if has_route is not None:
        with op.get_context().autocommit_block():
            after = 0
            while True:
                deleted = (
                    bind.execute(
                        sa.text(_DELETE_REST_BATCH), {"after": after, "limit": _DOWNGRADE_BATCH}
                    )
                    .scalars()
                    .all()
                )
                if not deleted:
                    break
                after = max(deleted)
    op.drop_index("ix_pmdv_day", table_name=_VISITORS, if_exists=True)
    op.drop_table(_VISITORS, if_exists=True)
    op.execute("ALTER TABLE public_mcp_usage_events DROP CONSTRAINT IF EXISTS valid_pmue_route")
    op.execute("ALTER TABLE public_mcp_usage_events DROP CONSTRAINT IF EXISTS valid_pmue_caller")
    op.drop_column(_TABLE, "route", if_exists=True)
    op.drop_column(_TABLE, "caller", if_exists=True)
