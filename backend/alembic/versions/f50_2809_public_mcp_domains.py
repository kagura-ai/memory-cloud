"""Verified custom domains of public MCP endpoints (#2809).

Creates ``public_mcp_domains`` and its indexes, nothing else: no data, no
foreign key, no change to any existing table. The revision therefore takes
no lock on any existing table; it can run while the application serves.

One row binds one host name to one publication of one workspace. The named
CHECKs keep the state machine's shape (a reason exactly while suspended,
both release times exactly while released, a lease on every active row, the
stored host-name form); the partial unique indexes keep one holder
(``active`` / ``suspended``) per host name and one open (not ``released``)
domain per publication.

Downgrade drops the table and every binding with it: every alias stops
being served and every claim, proof state and cooldown is lost.

Revision ID: f50_2809_public_mcp_domains
Revises: f48_2807_sync_feed_runs
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f50_2809_public_mcp_domains"
down_revision: str | None = "f48_2807_sync_feed_runs"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "public_mcp_domains"
_DB_NOW = "timezone('UTC', statement_timestamp())"


def upgrade() -> None:
    """Create ``public_mcp_domains`` and its indexes."""
    op.create_table(
        "public_mcp_domains",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("suspended_reason", sa.String(length=16), nullable=True),
        sa.Column("verification_token", sa.String(length=64), nullable=False),
        sa.Column("serve_until", sa.DateTime(), nullable=True),
        sa.Column("check_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_proof_result", sa.String(length=16), nullable=True),
        sa.Column("routing_status", sa.String(length=16), nullable=True),
        sa.Column("proof_ok_at", sa.DateTime(), nullable=True),
        sa.Column(
            "proof_missing_checks", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text(_DB_NOW)),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("suspended_at", sa.DateTime(), nullable=True),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'active', 'suspended', 'released')",
            name="ck_public_mcp_domains_state",
        ),
        sa.CheckConstraint(
            "suspended_reason IN ('proof_lost', 'right_ended', 'reserved')",
            name="ck_public_mcp_domains_suspended_reason",
        ),
        sa.CheckConstraint(
            "(state = 'suspended') = (suspended_reason IS NOT NULL)",
            name="ck_public_mcp_domains_suspended",
        ),
        sa.CheckConstraint(
            "(state = 'released') = (released_at IS NOT NULL AND cooldown_until IS NOT NULL)",
            name="ck_public_mcp_domains_released",
        ),
        sa.CheckConstraint(
            "state <> 'active' OR (serve_until IS NOT NULL AND verified_at IS NOT NULL "
            "AND proof_ok_at IS NOT NULL)",
            name="ck_public_mcp_domains_active_lease",
        ),
        sa.CheckConstraint(
            "hostname = lower(hostname) AND hostname NOT LIKE '%.' "
            "AND char_length(hostname) BETWEEN 4 AND 234",
            name="ck_public_mcp_domains_hostname",
        ),
        sa.CheckConstraint(
            "last_proof_result IN ('ok', 'missing', 'unavailable')",
            name="ck_public_mcp_domains_last_proof_result",
        ),
        sa.CheckConstraint(
            "routing_status IN ('ok', 'mismatch', 'unresolved')",
            name="ck_public_mcp_domains_routing_status",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ux_public_mcp_domains_hostname_held",
        _TABLE,
        ["hostname"],
        unique=True,
        postgresql_where=sa.text("state IN ('active', 'suspended')"),
        if_not_exists=True,
    )
    op.create_index(
        "ux_public_mcp_domains_endpoint_open",
        _TABLE,
        ["endpoint_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'released'"),
        if_not_exists=True,
    )
    op.create_index("ix_public_mcp_domains_hostname", _TABLE, ["hostname"], if_not_exists=True)
    op.create_index(
        "ix_public_mcp_domains_workspace_id", _TABLE, ["workspace_id"], if_not_exists=True
    )
    op.create_index(
        "ix_public_mcp_domains_due",
        _TABLE,
        ["last_checked_at"],
        postgresql_where=sa.text("state IN ('active', 'suspended')"),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop the table: every binding, claim and cooldown is lost."""
    op.drop_table(_TABLE)
