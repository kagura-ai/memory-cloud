"""Add erasure_requests table for GDPR Art.17 / APPI right-to-erasure.

Issue #360: ユーザー自身によるアカウント削除 API + admin 強制削除の両フローで
共有する受領→cooling-off→実行→完了の workflow テーブル。pseudonymized 監査
記録として 5 年保持し、バックアップ復元時の re-apply hook の起点としても使う
(CLO follow-up pin #1 / #2)。

Schema highlights:
- ``user_id`` is the OAuth2 ``sub`` (varchar) used everywhere as the universal
  cross-table key — there is no FK to users.user_id by repo convention
  (every other table stores it as plain VARCHAR with no cascade).
- ``user_email_hash`` is SHA256 of the user's email at request time. Plaintext
  email is never stored on this row; the audit trail proves a request existed
  for that email digest without keeping recoverable PII.
- ``confirm_token_hash`` is SHA256 of the one-time token; the raw token lives
  only in Redis (TTL 1h) and never on disk. Mirrors the api_keys.key_hash
  pattern (Argon2 / SHA256 store-only-the-hash discipline).
- ``status`` and ``reason_code`` use ``String + CheckConstraint`` per repo
  convention (no native PostgreSQL enums anywhere in this codebase).
- 5-year retention is documented in the table COMMENT and the ops runbook.
  No DB-level retention enforcement; it's an operational policy.

The partial index ``ix_erasure_requests_pending_sweep`` is what the cron
sweep query joins against — keeping it filtered to ``status='cooling_off'``
keeps the index size proportional to the in-flight queue rather than the
audit history (which dominates the table over time given 5-year retention).

Revision ID: c01_360_erasure_requests
Revises: b06_406_embedding_calibration

NOTE: Revision ID is 24 chars (alembic_version.version_num is VARCHAR(32) —
asyncpg raises StringDataRightTruncationError otherwise).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c01_360_erasure_requests"
down_revision: str | Sequence[str] | None = "b06_406_embedding_calibration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create erasure_requests table with constraints and indexes."""
    op.create_table(
        "erasure_requests",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # The user being erased (OAuth2 sub). Not a FK — repo convention.
        # Indexed via the explicit op.create_index below to give the index
        # a deterministic name (avoids the SQLAlchemy auto-name colliding
        # with op.create_index inside the same migration).
        sa.Column("user_id", sa.String(255), nullable=False),
        # SHA256 of the user's email at request time. Plaintext email is
        # never persisted here — the digest proves a request existed for
        # that email without storing recoverable PII.
        sa.Column("user_email_hash", sa.String(64), nullable=False),
        # Who triggered the request: self user_id (self-service) or admin
        # user_id (admin force-erase). Lets audit distinguish the two paths.
        sa.Column("initiated_by", sa.String(255), nullable=False),
        sa.Column(
            "is_self_service",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("reason_code", sa.String(50), nullable=False),
        # Free-form admin-supplied detail. App layer enforces max 1000 chars
        # so a UI textarea limit and the constraint stay in sync without
        # a DB-side length cap that would silently truncate.
        sa.Column("reason_detail", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        # SHA256 of the one-time confirmation token. The raw token only
        # ever lives in Redis (key: erasure_token:{token}, TTL 1h) and
        # never on disk. Mirrors api_keys.key_hash discipline.
        sa.Column("confirm_token_hash", sa.String(128), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text, nullable=True),
        # Per-store / per-table row count summary captured at completion
        # time. Schema (matches AccountErasureService._execute exactly):
        #   {"postgres": {"users": 1, "memories": 42, "api_keys": 3, ...},
        #    "qdrant":   {"kagura_memories": 38},
        #    "redis":    {"sessions": 2, "co_act": 17, "rate_limit": 1},
        #    "stripe":   {"workspaces_processed": [
        #        {"workspace_id": "<uuid>",
        #         "subscription_cancelled": true,
        #         "customer_deleted": true}
        #    ]},
        #    "workspaces": {"transferred": [...], "sole_owner_workspaces": <n>},
        #    "audit_logs_pseudonymized": <n>}
        # JSONB so admin tooling can query keys without unmarshaling.
        sa.Column("deleted_data_summary", JSONB, nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'cooling_off', 'in_progress', "
            "'complete', 'failed', 'cancelled')",
            name="valid_erasure_status",
        ),
        sa.CheckConstraint(
            "reason_code IN ('self_service', 'user_request_via_support', "
            "'legal_order', 'inactivity_policy', 'abuse_violation', 'other')",
            name="valid_erasure_reason_code",
        ),
        # Self-service rows must use the self_service reason_code. Keeps
        # admin-only reason_codes (legal_order, abuse_violation, ...) out of
        # the user-driven flow and vice versa.
        sa.CheckConstraint(
            "(is_self_service = true AND reason_code = 'self_service') OR "
            "(is_self_service = false AND reason_code <> 'self_service')",
            name="erasure_self_service_reason_consistency",
        ),
    )

    # Lookup-by-user (admin browsing the audit history of a deleted user).
    op.create_index("ix_erasure_requests_user_id", "erasure_requests", ["user_id"])

    # General status query for admin dashboard.
    op.create_index("ix_erasure_requests_status", "erasure_requests", ["status"])

    # Partial index for the cron sweep: only cooling_off rows whose
    # scheduled_for has passed. Stays small (in-flight queue size) rather
    # than growing with the full 5-year audit history.
    op.create_index(
        "ix_erasure_requests_pending_sweep",
        "erasure_requests",
        ["scheduled_for"],
        postgresql_where=sa.text("status = 'cooling_off'"),
    )

    # At most one active erasure request per user. Without this, two
    # concurrent confirms can both pass the in-memory status check and
    # commit two cooling_off rows for the same user, leading to double
    # execution. Partial uniqueness on the active states (pending,
    # cooling_off, in_progress) lets historical failed/cancelled/complete
    # rows accumulate freely (5-year audit retention).
    op.create_index(
        "uq_erasure_one_active_per_user",
        "erasure_requests",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'cooling_off', 'in_progress')"),
    )

    # Document the retention policy in the DB itself so future maintainers
    # don't accidentally add a "cleanup old rows" job.
    op.execute(
        "COMMENT ON TABLE erasure_requests IS "
        "'GDPR Art.5(1)(b) / Art.30 accountability evidence. "
        "Retain 5 years. Pseudonymized by design "
        "(no plaintext PII besides hashes). Issue #360.'"
    )


def downgrade() -> None:
    """Drop erasure_requests table."""
    op.drop_index("uq_erasure_one_active_per_user", table_name="erasure_requests")
    op.drop_index("ix_erasure_requests_pending_sweep", table_name="erasure_requests")
    op.drop_index("ix_erasure_requests_status", table_name="erasure_requests")
    op.drop_index("ix_erasure_requests_user_id", table_name="erasure_requests")
    op.drop_table("erasure_requests")
