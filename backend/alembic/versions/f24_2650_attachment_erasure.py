"""Reclaim legacy file objects with their owning attachment (#2650).

Attachment deletion covers memory purges, account erasure and FK cascades.
The existing file DELETE trigger queues each storage key in the durable R2
outbox. Reverting this revision cannot restore bytes already erased.
"""

from alembic import op

revision = "f24_2650_attachment_erasure"
down_revision = "f23_2649_attachment_dedup"
branch_labels = None
depends_on = None

DDL = (
    """CREATE INDEX ix_file_objects_legacy_creator ON file_objects (created_by)
       WHERE created_by LIKE 'migration:attachment:%'""",
    """CREATE FUNCTION erase_migrated_attachment_files(ids uuid[]) RETURNS void
       LANGUAGE sql AS $$
       WITH removed AS (
           DELETE FROM file_objects WHERE id IN (
               SELECT id FROM file_objects WHERE id = ANY(ids)
                 AND created_by LIKE 'migration:attachment:%'
               ORDER BY id FOR UPDATE
           )
           RETURNING workspace_id, size_bytes, status, deleted_at
       ), totals AS (
           SELECT workspace_id, sum(size_bytes) AS bytes, count(*) AS files
           FROM removed WHERE status = 'uploaded' AND deleted_at IS NULL
           GROUP BY workspace_id
       )
       UPDATE workspace_storage_usage u
       SET used_bytes = greatest(0, u.used_bytes - totals.bytes),
           file_count = greatest(0, u.file_count - totals.files),
           updated_at = timezone('UTC', now())
       FROM totals WHERE u.workspace_id = totals.workspace_id
       $$""",
    """CREATE FUNCTION erase_attachment_copies() RETURNS trigger LANGUAGE plpgsql AS $$
       DECLARE ids uuid[];
       BEGIN
           SELECT array_agg(id ORDER BY id) INTO ids FROM file_objects
           WHERE created_by LIKE 'migration:attachment:%'
             AND created_by = 'migration:attachment:' || OLD.id;
           IF ids IS NOT NULL THEN
               PERFORM erase_migrated_attachment_files(ids);
           END IF;
           RETURN NULL;
       END $$""",
    """CREATE TRIGGER attachment_delete_copies AFTER DELETE ON attachments
       FOR EACH ROW EXECUTE FUNCTION erase_attachment_copies()""",
)


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER attachment_delete_copies ON attachments")
    op.execute("DROP FUNCTION erase_attachment_copies()")
    op.execute("DROP FUNCTION erase_migrated_attachment_files(uuid[])")
    op.drop_index("ix_file_objects_legacy_creator", table_name="file_objects")
