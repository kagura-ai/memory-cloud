"""#2092: add memories.embedded_text_digest and backfill it on success rows.

The digest of the summary + content a row was last successfully embedded with
(``services.supersede_verify.embedded_text_digest``: ``md5(summary + "\\x1f" +
content)``, 32 hex). ``process_pending_embedding`` compares it with the text it
just embedded: equal means a mechanical re-embed (content-chunk backfill,
embedding-model switch, repair re-queue), which runs no supersede detection and
no judge call. Only the pipeline's success write sets it.

Nullable, no default: ADD COLUMN is metadata-only on PG 11+. The backfill
stamps every ``success`` row with the SQL twin ``md5(summary || chr(31) ||
content)`` — ``md5(text)`` hashes the UTF-8 bytes under the UTF8 server
encoding, byte-identical to the Python side — so the first re-queue after the
deploy already skips. Soft-deleted rows are included (a restored context comes
back with the same text). Rows pending / processing / failed now stay NULL:
their next run detects and stamps.

The UPDATE rewrites every ``success`` row once, in the upgrade's single
transaction; an operator with a very large ``memories`` table runs the upgrade
off-peak. Both steps are re-runnable.

Revision ID: f11_2092_embedded_text_digest
Revises: f10_2115_revisions_actor_idx
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "f11_2092_embedded_text_digest"
down_revision = "f10_2115_revisions_actor_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable digest column and stamp the rows already embedded."""
    op.add_column(
        "memories",
        sa.Column("embedded_text_digest", sa.String(32), nullable=True),
        if_not_exists=True,
    )
    op.execute(
        "UPDATE memories SET embedded_text_digest = md5(summary || chr(31) || content) "
        "WHERE embedding_status = 'success'"
    )


def downgrade() -> None:
    """Drop embedded_text_digest."""
    op.drop_column("memories", "embedded_text_digest")
