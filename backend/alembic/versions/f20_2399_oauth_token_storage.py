"""Install OAuth hashing bridge; finalize only after draining old processes (#2399)."""

import sqlalchemy as sa

from alembic import op

revision: str = "f20_2399_oauth_token_storage"
down_revision: str | None = "f19_2391_embedding_erasure"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_token_storage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_oauth_token_storage_singleton"),
        sa.CheckConstraint("mode IN ('bridge', 'hashed')", name="ck_oauth_token_storage_mode"),
        if_not_exists=True,
    )
    # Never change an existing mode on a retried upgrade. Even an empty database
    # starts bridge: old binaries may still issue tokens after migration commits.
    op.execute(
        "INSERT INTO oauth_token_storage (id, mode) VALUES (1, 'bridge') ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    # Hashing is irreversible. Refuse to make a finalized DB appear safe for
    # old binaries; restoring a pre-finalization backup is an operator decision.
    mode = (
        op.get_bind()
        .execute(sa.text("SELECT mode FROM oauth_token_storage WHERE id = 1"))
        .scalar_one()
    )
    if mode == "hashed":
        raise RuntimeError(
            "OAuth token hashing was finalized; downgrade to legacy readers is unsafe"
        )
    op.drop_table("oauth_token_storage")
