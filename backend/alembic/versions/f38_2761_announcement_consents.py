"""#2761: ``announcement_email_consents`` — who wants announcement emails.

Append-only history: one row per recorded answer (value, wording version,
source, time). The user's current answer is the value on their newest row
(highest ``id``); ``users`` gets no column. A user with no row has never
answered and gets no announcement email.

- ``id`` is a BIGSERIAL: insertion order breaks the tie between two rows
  written in one transaction.
- ``user_id`` is ``ON DELETE CASCADE`` — the history goes with the account.
- ``source`` is CHECK-constrained to ``signup`` / ``settings`` /
  ``unsubscribe_link``.
- a ``true`` row must name the wording version the person saw.
- ``(user_id, id)`` serves the "newest row for this user" read and the
  recipient scan.

A new, empty table: nothing to backfill (every existing user starts as
"never answered").

Downgrade drops the table (the recorded history is lost).

Revision ID: f38_2761_announcement_consents
Revises: f37_1734_email_signup

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f38_2761_announcement_consents"
down_revision: str | None = "f37_1734_email_signup"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``announcement_email_consents`` and its per-user index."""
    op.create_table(
        "announcement_email_consents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Boolean(), nullable=False),
        sa.Column("wording_version", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "source IN ('signup', 'settings', 'unsubscribe_link')",
            name="valid_announcement_consent_source",
        ),
        sa.CheckConstraint(
            "value = false OR wording_version IS NOT NULL",
            name="ck_announcement_consent_version_when_on",
        ),
    )
    op.create_index(
        "ix_announcement_email_consents_user_id_id",
        "announcement_email_consents",
        ["user_id", "id"],
    )


def downgrade() -> None:
    """Drop ``announcement_email_consents``."""
    op.drop_index(
        "ix_announcement_email_consents_user_id_id",
        table_name="announcement_email_consents",
    )
    op.drop_table("announcement_email_consents")
