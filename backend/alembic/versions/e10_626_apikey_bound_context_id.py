"""Add ``bound_context_id`` to api_keys + ``api_key_id`` to usage_stats (#626).

Extends the existing public read endpoint (``POST /api/v1/public/{ctx}/search``)
with optional API-key attribution. A key whose ``bound_context_id`` is set
restricts to that one ``is_public=true`` context; absence preserves the
existing owner-scoped / workspace-scoped (#169) semantics.

Schema changes:

1. ``api_keys.bound_context_id`` — UUID, nullable, indexed. References
   ``contexts.id`` with ``ON DELETE SET NULL`` so that deleting the bound
   context disables the key without cascading the key's deletion (audit
   trail of the lifecycle stays intact).
2. ``api_keys.bound_context_id`` and ``api_keys.workspace_id`` are mutually
   exclusive via a CHECK constraint. A key cannot simultaneously be
   workspace-scoped (#169 — full access to all contexts in a workspace)
   AND public-bound (single-context attribution); the two scopings have
   contradictory semantics.
3. ``usage_stats.api_key_id`` — Integer, nullable, indexed. NOT a foreign
   key on purpose: we want attribution rows to survive key deletion so
   billing/audit reports remain readable; the api_keys.id reference
   becomes a soft dangling pointer post-delete (acceptable for analytics).

The binding itself is immutable (no update endpoint at the route layer).
To change which context a key is attributed to, the operator revokes the
old key and creates a new one. This keeps the audit story simple.

Revision ID: e10_626_apikey_bound_context_id
Revises: e09_608_dcr_default_narrow
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "e10_626_apikey_bound_context_id"
down_revision = "e09_608_dcr_default_narrow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # api_keys.bound_context_id with FK to contexts.id and SET NULL on delete.
    op.add_column(
        "api_keys",
        sa.Column(
            "bound_context_id",
            UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_api_keys_bound_context_id",
        "api_keys",
        "contexts",
        ["bound_context_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_api_keys_bound_context_id",
        "api_keys",
        ["bound_context_id"],
    )

    # Mutual exclusion: a key cannot be both workspace-scoped (#169) and
    # public-bound (#626). Either both NULL (global key) or exactly one
    # non-NULL (workspace or public-bound) is permitted.
    op.create_check_constraint(
        "ck_api_keys_binding_workspace_exclusion",
        "api_keys",
        "bound_context_id IS NULL OR workspace_id IS NULL",
    )

    # usage_stats.api_key_id — soft reference (no FK) for attribution that
    # survives key deletion.
    op.add_column(
        "usage_stats",
        sa.Column("api_key_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "idx_usage_stats_api_key_id",
        "usage_stats",
        ["api_key_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_usage_stats_api_key_id", table_name="usage_stats")
    op.drop_column("usage_stats", "api_key_id")

    op.drop_constraint(
        "ck_api_keys_binding_workspace_exclusion",
        "api_keys",
        type_="check",
    )
    op.drop_index("idx_api_keys_bound_context_id", table_name="api_keys")
    op.drop_constraint(
        "fk_api_keys_bound_context_id",
        "api_keys",
        type_="foreignkey",
    )
    op.drop_column("api_keys", "bound_context_id")
