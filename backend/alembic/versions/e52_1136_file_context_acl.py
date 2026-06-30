"""file_objects.context_id for context-scoped file access control (#1136).

Adds a nullable ``context_id`` FK to ``file_objects`` so file access can be
routed through the owning context's ACL (private/shared, ``allowed_context_ids``,
per-context role) instead of the flat workspace role. ``NULL`` = workspace-scoped
(legacy behaviour); existing rows are left ``NULL`` — no backfill, so no file's
visibility changes on upgrade. Quota + sha256 dedup stay workspace-level; only
*access* becomes context-scoped, so the existing
``uq_file_objects_workspace_sha256_active`` partial-unique index is unchanged.

``ON DELETE SET NULL``: contexts soft-delete (#84), so this FK only fires on a
workspace hard-delete cascade (where the ``file_objects`` row is removed via its
own workspace FK anyway) — a graceful fallback to workspace-scope, never a
dangling reference.

Chained after ``e51_1153_secret_delete`` (the #1153 secret-delete migration,
merged to main first) so the history stays a single linear head
``e50 → e51 → e52`` — both were authored in parallel off ``e50`` and reconciled
at merge time.

Revision ID: e52_1136_file_context_acl
Revises: e51_1153_secret_delete
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "e52_1136_file_context_acl"
down_revision = "e51_1153_secret_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "file_objects",
        sa.Column("context_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_file_objects_context_id",
        "file_objects",
        "contexts",
        ["context_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_file_objects_context_id", "file_objects", ["context_id"])


def downgrade() -> None:
    op.drop_index("ix_file_objects_context_id", table_name="file_objects")
    op.drop_constraint("fk_file_objects_context_id", "file_objects", type_="foreignkey")
    op.drop_column("file_objects", "context_id")
