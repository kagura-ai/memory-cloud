"""Durable object cleanup across file/workspace cascades (#2395).

Statement transition tables acquire shared-key locks and outbox row locks in
key order. Triggers also protect writes made by the previous API version during
a rolling deploy. Only the exact object key and timestamps are retained; no account metadata.
"""

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import index_is_invalid

revision: str = "f18_2395_file_cleanup"
down_revision: str | None = "f17_2298_audit_logs_user_id_idx"
branch_labels: str | None = None
depends_on: str | None = None

DDL = (
    """CREATE FUNCTION lock_file_object_keys() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE object_key text;
    BEGIN
        FOR object_key IN SELECT DISTINCT storage_key FROM new_files
            WHERE storage_key IS NOT NULL ORDER BY storage_key
        LOOP
            PERFORM pg_advisory_xact_lock(hashtextextended(object_key, 2395));
        END LOOP;
        RETURN NULL;
    END $$""",
    """CREATE TRIGGER file_object_insert_keys AFTER INSERT ON file_objects
    REFERENCING NEW TABLE AS new_files FOR EACH STATEMENT
    EXECUTE FUNCTION lock_file_object_keys()""",
    """CREATE TRIGGER file_object_update_keys AFTER UPDATE ON file_objects
    REFERENCING NEW TABLE AS new_files FOR EACH STATEMENT
    EXECUTE FUNCTION lock_file_object_keys()""",
    """CREATE FUNCTION enqueue_deleted_file_objects() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        INSERT INTO storage_object_deletions (storage_key, available_after)
        SELECT storage_key,
            greatest(timezone('UTC', now()), max(expires_at) + interval '1 hour')
        FROM old_files WHERE storage_key IS NOT NULL
        GROUP BY storage_key ORDER BY storage_key
        ON CONFLICT (storage_key) DO UPDATE SET available_after =
            greatest(storage_object_deletions.available_after, EXCLUDED.available_after);
        RETURN NULL;
    END $$""",
    """CREATE TRIGGER file_object_delete_cleanup AFTER DELETE ON file_objects
    REFERENCING OLD TABLE AS old_files FOR EACH STATEMENT
    EXECUTE FUNCTION enqueue_deleted_file_objects()""",
)


def upgrade() -> None:
    invalid = index_is_invalid("ix_file_objects_storage_key")
    with op.get_context().autocommit_block():
        if invalid:
            op.drop_index(
                "ix_file_objects_storage_key",
                table_name="file_objects",
                postgresql_concurrently=True,
            )
        op.create_index(
            "ix_file_objects_storage_key",
            "file_objects",
            ["storage_key"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
    op.create_table(
        "storage_object_deletions",
        sa.Column("storage_key", sa.String(1024), primary_key=True),
        sa.Column("available_after", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_storage_object_deletions_available_after",
        "storage_object_deletions",
        ["available_after"],
    )
    for statement in DDL:
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER file_object_delete_cleanup ON file_objects"))
    op.execute(sa.text("DROP TRIGGER file_object_update_keys ON file_objects"))
    op.execute(sa.text("DROP TRIGGER file_object_insert_keys ON file_objects"))
    op.execute(sa.text("DROP FUNCTION enqueue_deleted_file_objects()"))
    op.execute(sa.text("DROP FUNCTION lock_file_object_keys()"))
    op.drop_table("storage_object_deletions")
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_file_objects_storage_key", table_name="file_objects", postgresql_concurrently=True
        )
