"""#2941: edge certificate reports of verified custom domains.

One new table, without a foreign key, and one index:

- ``public_mcp_domain_tls_reports``: one row per domain claim (keyed by the
  claim's id), the latest report of the certificate operator about it: the
  host name and endpoint id it names, a status (CHECK in five values), when it
  was observed, until when it is evidence, the certificate's end when known
  and when it was received; indexed by ``valid_until`` for the cleanup.

Nothing is backfilled. No existing table is altered or referenced, so the
revision takes no lock on an existing table: ``CREATE TABLE`` and the index
build lock only the new, empty table (and the ``alembic_version`` row every
revision updates). It is safe while the previous release serves traffic. While
the feature is not configured (``MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN`` empty,
or ``MCP_PUBLIC_DOMAINS_ENABLED`` / ``MCP_PUBLIC_ENDPOINTS_ENABLED`` off) no
code reads or writes the table. Code and migration may therefore be deployed
in either order.

``if_not_exists`` makes a rerun after an interrupted upgrade a no-op.

Downgrade drops the table and the stored reports with it; every report is
finite evidence that the reporter sends again.

Revision ID: f58_2941_domain_tls_reports
Revises: f56_2918_workspace_slots
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f58_2941_domain_tls_reports"
down_revision: str | None = "f56_2918_workspace_slots"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "public_mcp_domain_tls_reports"
_VALID_UNTIL_INDEX = "ix_public_mcp_domain_tls_reports_valid_until"


def upgrade() -> None:
    """Create the report table and its index (no existing table touched)."""
    op.create_table(
        _TABLE,
        sa.Column("domain_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("certificate_not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("domain_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'valid', 'issuance_failed', 'renewal_failed', 'expired')",
            name="ck_public_mcp_domain_tls_report_status",
        ),
        if_not_exists=True,
    )
    op.create_index(_VALID_UNTIL_INDEX, _TABLE, ["valid_until"], if_not_exists=True)


def downgrade() -> None:
    """Drop the report table (every stored report goes with it)."""
    op.drop_index(_VALID_UNTIL_INDEX, table_name=_TABLE, if_exists=True)
    op.drop_table(_TABLE, if_exists=True)
