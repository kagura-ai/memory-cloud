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

On a FIPS-mode server ``md5()`` raises (#2511): the backfill first probes
``md5('')``, and on that error (PG 15+ ``internal_error``, PG 14
``out_of_memory``) raises a WARNING (kept in the server log; the upgrade's
asyncpg connection prints no server message) and skips the UPDATE, so the rows
stay NULL and detect once on their next re-queue. Any other error, and any
error of the UPDATE itself, aborts the upgrade.

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

# The probe runs in a sub-transaction of its own; the UPDATE does not, so a
# failure there still rolls the whole upgrade back.
_GUARDED_BACKFILL = """
DO $$
BEGIN
    BEGIN
        PERFORM md5('');
    EXCEPTION WHEN internal_error OR out_of_memory THEN
        RAISE WARNING 'f11 skipped the embedded_text_digest backfill, md5() failed (%)', SQLERRM;
        RETURN;
    END;
    UPDATE memories SET embedded_text_digest = md5(summary || chr(31) || content)
    WHERE embedding_status = 'success';
END
$$
"""


def upgrade() -> None:
    """Add the nullable digest column and stamp the rows already embedded."""
    op.add_column(
        "memories",
        sa.Column("embedded_text_digest", sa.String(32), nullable=True),
        if_not_exists=True,
    )
    op.execute(_GUARDED_BACKFILL)


def downgrade() -> None:
    """Drop embedded_text_digest."""
    op.drop_column("memories", "embedded_text_digest")
