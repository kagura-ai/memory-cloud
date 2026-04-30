"""Drop NOT NULL on oauth_clients.owner_id (#519, #513 follow-up).

Issue #513 (PR #518) added Dynamic Client Registration (RFC 7591) and
adjusted the response model so DCR-registered clients (which have no human
owner) can serialize ``owner_id=None``. The underlying SQLAlchemy column
and DB constraint were left as ``NOT NULL``, so any successful DCR INSERT
fails with::

    psycopg2.errors.NotNullViolation: null value in column "owner_id"
    of relation "oauth_clients" violates not-null constraint

DCR rejection paths still work end-to-end (they short-circuit before the
INSERT), but DCR accept paths are broken in production. This migration
aligns the DB constraint with the model + response shape so DCR can
register a public client with no owner.

Admin-managed clients (``POST /api/v1/oauth/clients``) continue to record
``owner_id=current_user_id``; the relaxation only opens the door for the
DCR path. The accompanying model change in ``models/auth.py`` flips
``OAuth2Client.owner_id`` to ``nullable=True``.

Downgrade re-applies ``SET NOT NULL``. Any existing rows with
``owner_id IS NULL`` (DCR-registered clients) will block the downgrade —
that is the correct fail-loud behavior; the operator must either delete
the DCR rows or backfill an owner before downgrading.

Revision ID: d04_519_oauth_owner_nullable
Revises: c03_471_seed_pricing

NOTE: Revision IDs are capped at 32 chars because
``alembic_version.version_num`` is ``VARCHAR(32)`` in this database
(asyncpg raises ``StringDataRightTruncationError`` otherwise).
The longer candidate ``d04_519_oauth_clients_owner_id_nullable`` is
39 chars — still over the cap — so this migration uses the shorter
``d04_519_oauth_owner_nullable`` (29 chars). The Python filename is
allowed to be longer than the revision id, so we keep the descriptive
filename for grep-ability.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d04_519_oauth_owner_nullable"
down_revision = "c03_471_seed_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop NOT NULL on oauth_clients.owner_id."""
    op.alter_column(
        "oauth_clients",
        "owner_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    """Restore NOT NULL on oauth_clients.owner_id.

    This will fail if any rows have ``owner_id IS NULL`` (DCR-registered
    clients). Operators must delete those rows or backfill an owner before
    downgrading.
    """
    op.alter_column(
        "oauth_clients",
        "owner_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
