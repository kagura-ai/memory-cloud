"""Per-channel destination contexts of a chat connector (#1759).

Adds ``workspace_connectors.unrouted_private_channels`` (``default`` | ``skip``,
NOT NULL, server default ``default``: existing connectors keep today's
behaviour) and the ``workspace_connector_channel_routes`` table (one row per
channel with a destination of its own, CASCADE with its connector, SET NULL
when the destination context is hard-deleted).

The column's CHECK is added ``NOT VALID`` and validated in a transaction of
its own (``validate_constraint``), so the steps before it are re-runnable; the
table comes after that commit and is created only when missing.

Revision ID: f35_1759_channel_routes
Revises: f34_2739_history_retention_addon
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from db.migration_helpers import validate_constraint

revision = "f35_1759_channel_routes"
down_revision = "f34_2739_history_retention_addon"
branch_labels = None
depends_on = None

_TABLE = "workspace_connector_channel_routes"


def upgrade() -> None:
    op.add_column(
        "workspace_connectors",
        sa.Column(
            "unrouted_private_channels",
            sa.String(16),
            nullable=False,
            server_default="default",
        ),
        if_not_exists=True,
    )
    op.execute(
        "ALTER TABLE workspace_connectors DROP CONSTRAINT IF EXISTS "
        "check_workspace_connectors_unrouted_private_channels"
    )
    op.execute(
        "ALTER TABLE workspace_connectors ADD CONSTRAINT "
        "check_workspace_connectors_unrouted_private_channels "
        "CHECK (unrouted_private_channels IN ('default', 'skip')) NOT VALID"
    )
    validate_constraint(
        "workspace_connectors", "check_workspace_connectors_unrouted_private_channels"
    )

    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "workspace_connectors.id",
                ondelete="CASCADE",
                name="fk_workspace_connector_channel_routes_connector_id",
            ),
            nullable=False,
        ),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column(
            "context_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "contexts.id",
                ondelete="SET NULL",
                name="fk_workspace_connector_channel_routes_context_id",
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "connector_id", "channel_id", name="uq_workspace_connector_channel_routes_channel"
        ),
        sa.CheckConstraint(
            "action IN ('route', 'skip')",
            name="check_workspace_connector_channel_routes_action",
        ),
        sa.CheckConstraint(
            "action = 'route' OR context_id IS NULL",
            name="check_workspace_connector_channel_routes_skip_no_context",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_workspace_connector_channel_routes_context_id",
        _TABLE,
        ["context_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Refused while any connector uses routing.

    A release before this one vends no routes: its worker config would send
    every routed or skipped channel, private ones included, into the default
    context. Clear the routes and set the switch back to ``default`` (settings
    PATCH ``{"channel_routes": [], "unrouted_private_channels": "default"}``)
    on every connector first.
    """
    in_use = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM workspace_connector_channel_routes UNION ALL "
                "SELECT 1 FROM workspace_connectors "
                "WHERE unrouted_private_channels <> 'default' LIMIT 1"
            )
        )
        .first()
    )
    if in_use is not None:
        raise RuntimeError(
            "f35_1759_channel_routes downgrade refused: connectors still use channel "
            "routing; clear channel_routes and set unrouted_private_channels to "
            "'default' on every connector first (#1759)"
        )
    op.drop_table(_TABLE, if_exists=True)
    op.execute(
        "ALTER TABLE workspace_connectors DROP CONSTRAINT IF EXISTS "
        "check_workspace_connectors_unrouted_private_channels"
    )
    op.drop_column("workspace_connectors", "unrouted_private_channels", if_exists=True)
