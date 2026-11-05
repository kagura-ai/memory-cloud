"""Keep migrated attachment ownership independent of upload hash dedup (#2649).

Downgrade fails atomically if live hashes now collide. Resolve them with
the data owner before retrying; never discard file rows in a downgrade.
"""

from alembic import op

revision = "f23_2649_attachment_dedup"
down_revision = "f22_2639_context_members_uq"
branch_labels = None
depends_on = None

_INDEX = "uq_file_objects_workspace_sha256_active"
_ACTIVE = "deleted_at IS NULL AND status <> 'failed'"


def _replace_index(predicate: str) -> None:
    op.drop_index(_INDEX, table_name="file_objects")
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX} ON file_objects (workspace_id, lower(sha256)) "
        f"WHERE {predicate}"
    )


def upgrade() -> None:
    _replace_index(f"{_ACTIVE} AND created_by NOT LIKE 'migration:attachment:%'")


def downgrade() -> None:
    _replace_index(_ACTIVE)
