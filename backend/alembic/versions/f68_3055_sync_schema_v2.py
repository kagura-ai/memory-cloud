"""#3055: the feed kinds of record schema version 2 (``page`` and ``review``).

Replaces the CHECK ``ck_sync_feed_kind`` on ``sync_feeds``: ``kind IN
('product', 'faq')`` becomes ``kind IN ('product', 'faq', 'page', 'review')``.
The new CHECK is added ``NOT VALID`` and validated in a transaction of its own.
No column, no backfill: an installation's schema version is its existing
``resource_schemas`` row.

Locks: the ``DROP CONSTRAINT`` and the ``ADD CONSTRAINT ... NOT VALID`` take a
brief ``ACCESS EXCLUSIVE`` lock on ``sync_feeds`` under the migration
``lock_timeout``; the commit point before the VALIDATE releases it. The
VALIDATE scan then holds ``SHARE UPDATE EXCLUSIVE`` on ``sync_feeds`` (reads
and writes continue). No lock is taken on ``users``, ``workspaces``,
``contexts`` or any other table.

Order: before or after the new code. The previous release writes only
``product`` and ``faq`` and keeps working against the wider CHECK; the new
code writes ``page`` or ``review`` only for an installation on schema version
2, which needs this revision.

Re-runnable (``DROP CONSTRAINT IF EXISTS``, ``ADD ... NOT VALID``).

Downgrade restores the two-kind CHECK. It refuses, before changing anything,
while a ``page`` or ``review`` feed exists: such a feed belongs to an
installation on schema version 2, which the previous release cannot serve.

Revision ID: f68_3055_sync_schema_v2
Revises: f67_3035_public_visitors
"""

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import validate_constraint

revision: str = "f68_3055_sync_schema_v2"
down_revision: str | None = "f67_3035_public_visitors"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "sync_feeds"
_CONSTRAINT = "ck_sync_feed_kind"
_DROP = "ALTER TABLE sync_feeds DROP CONSTRAINT IF EXISTS ck_sync_feed_kind"
_ADD_VERSION_2_KINDS = (
    "ALTER TABLE sync_feeds ADD CONSTRAINT ck_sync_feed_kind "
    "CHECK (kind IN ('product', 'faq', 'page', 'review')) NOT VALID"
)
_ADD_VERSION_1_KINDS = (
    "ALTER TABLE sync_feeds ADD CONSTRAINT ck_sync_feed_kind "
    "CHECK (kind IN ('product', 'faq')) NOT VALID"
)


def upgrade() -> None:
    op.execute(_DROP)
    op.execute(_ADD_VERSION_2_KINDS)
    validate_constraint(_TABLE, _CONSTRAINT)


def downgrade() -> None:
    leftover = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM sync_feeds WHERE kind IN ('page', 'review') LIMIT 1"))
        .first()
    )
    if leftover is not None:
        raise RuntimeError(
            "sync_feeds holds page or review feeds (schema version 2); "
            "the previous release cannot serve them, so this downgrade stops"
        )
    op.execute(_DROP)
    op.execute(_ADD_VERSION_1_KINDS)
    validate_constraint(_TABLE, _CONSTRAINT)
