"""Add user_oauth_providers table + provider backfill (#517).

Multi-provider OAuth account linking. Maps an OAuth identity
``(provider, oauth_sub)`` to its owning user. Two composite UNIQUE
constraints preserve the invariants: an OAuth identity belongs to at most
one user (``provider, oauth_sub``), and a user links a given provider at
most once (``user_id, provider``). The FK cascades on delete so links are
erased with the user (GDPR/APPI erasure).

Backfill (edge case 4 from gate1): one row per existing user whose
``auth_provider IN ('google', 'github')`` AND ``auth_method <> 'password'``,
using the user's own ``user_id`` as ``oauth_sub`` (the pre-#517 single-
provider invariant: sub == user_id). Users with ``auth_provider IS NULL``
(pre-#361 legacy) or password auth are deliberately SKIPPED — they resolve
via the ensure_user dual-read fallback and self-heal on next login.
``ON CONFLICT DO NOTHING`` makes the backfill idempotent on re-run.
"""

import logging

import sqlalchemy as sa

from alembic import op

revision = "e37_517_user_oauth_providers"
down_revision = "e36_888_retrieval_feedback"
branch_labels = None
depends_on = None

# Use the alembic logger so output goes through the standard migration
# logging config (see backend/alembic.ini) rather than bypassing it.
logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.create_table(
        "user_oauth_providers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=255),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("oauth_sub", sa.String(length=255), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("provider", "oauth_sub", name="uq_user_oauth_providers_provider_sub"),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_oauth_providers_user_provider"),
        sa.CheckConstraint("provider IN ('google', 'github')", name="valid_oauth_provider"),
    )
    op.create_index(
        "ix_user_oauth_providers_user_id",
        "user_oauth_providers",
        ["user_id"],
    )

    # Backfill existing OAuth users. The pre-#517 invariant is that a user's
    # OAuth ``sub`` equals their ``user_id`` (single-provider era), so the
    # provider link's ``oauth_sub`` is the user's own id. ``COALESCE`` guards
    # against a NULL ``created_at`` (none expected — server_default now() —
    # but defensive). Skip NULL-provider (pre-#361) and password-auth users.
    conn = op.get_bind()
    inserted = conn.execute(
        sa.text(
            """
            INSERT INTO user_oauth_providers (user_id, provider, oauth_sub, linked_at)
            SELECT user_id, auth_provider, user_id, COALESCE(created_at, now())
            FROM users
            WHERE auth_provider IN ('google', 'github')
              AND auth_method <> 'password'
            ON CONFLICT DO NOTHING
            """
        )
    ).rowcount
    skipped = conn.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM users
            WHERE auth_provider IS NULL OR auth_method = 'password'
            """
        )
    ).scalar()
    logger.info(
        "[#517] backfilled %s user_oauth_providers row(s); "
        "skipped %s NULL-provider / password-auth user(s) (self-heal on next login)",
        inserted or 0,
        skipped or 0,
    )


def downgrade() -> None:
    op.drop_index("ix_user_oauth_providers_user_id", table_name="user_oauth_providers")
    op.drop_table("user_oauth_providers")
