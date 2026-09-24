"""#1665: ``terms_acceptances`` — who accepted which terms version, and when.

Append-only history: one row per recorded acceptance. The user's accepted
version is the version on their newest row, so ``users`` gets no column.

- ``user_id`` is ``ON DELETE CASCADE`` — the history goes with the account.
- ``source`` is CHECK-constrained to the four entry points that record an
  acceptance (``login`` / ``join`` / ``password`` / ``reaccept``).
- ``accepted_at`` is ``TIMESTAMP WITH TIME ZONE`` (default ``now()``).
- ``(user_id, accepted_at)`` serves the "newest row for this user" read.

A new, empty table: nothing to backfill. Rows are written only while the
deployment sets ``TERMS_VERSION``.

Downgrade drops the table (the recorded history is lost).

Revision ID: e85_1665_terms_acceptances
Revises: e84_1619_tool_guardrails

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e85_1665_terms_acceptances"
down_revision: str | None = "e84_1619_tool_guardrails"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``terms_acceptances`` and its per-user index."""
    op.create_table(
        "terms_acceptances",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "source IN ('login', 'join', 'password', 'reaccept')",
            name="valid_terms_acceptance_source",
        ),
    )
    op.create_index(
        "ix_terms_acceptances_user_accepted_at",
        "terms_acceptances",
        ["user_id", "accepted_at"],
    )


def downgrade() -> None:
    """Drop ``terms_acceptances``."""
    op.drop_index("ix_terms_acceptances_user_accepted_at", table_name="terms_acceptances")
    op.drop_table("terms_acceptances")
