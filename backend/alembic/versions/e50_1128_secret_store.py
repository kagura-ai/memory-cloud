"""Zero-knowledge secret store: recipient_pubkeys, secrets, secret_versions,
secret_grants, secret_access_log (#1128).

A passive, ciphertext-only store for devops API keys. The server holds public
``age`` recipient keys + opaque ciphertext only and never decrypts. Tables are
workspace-scoped and isolated from the memory/recall surface (their own models,
no ``embedding_status`` column).

``secret_access_log`` is append-only AND tamper-evident:
- A ``BEFORE UPDATE OR DELETE`` trigger blocks any row mutation at the DB level
  (application discipline alone is not trusted — this is the gate1 finding).
- A per-workspace HMAC hash chain (``prev_hash`` → ``entry_hash``) makes any
  out-of-band tampering independently detectable; the ``(workspace_id, prev_hash)``
  unique constraint keeps the chain strictly linear (no forks).

Revision ID: e50_1128_secret_store
Revises: e49_1096_drop_stripe_cols
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "e50_1128_secret_store"
down_revision = "e49_1096_drop_stripe_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- recipient_pubkeys -----------------------------------------------------
    op.create_table(
        "recipient_pubkeys",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("identity_id", sa.String(length=255), nullable=False),
        sa.Column("pubkey", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("attested_at", sa.DateTime(), nullable=True),
        sa.Column("attested_by", sa.String(length=255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_recipient_pubkeys_workspace_id",
        ),
        sa.UniqueConstraint("workspace_id", "pubkey", name="uq_recipient_pubkeys_ws_pubkey"),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revoked')",
            name="valid_recipient_pubkey_status",
        ),
    )
    op.create_index(
        "ix_recipient_pubkeys_ws_identity",
        "recipient_pubkeys",
        ["workspace_id", "identity_id"],
    )
    op.create_index("ix_recipient_pubkeys_fingerprint", "recipient_pubkeys", ["fingerprint"])

    # --- secrets ---------------------------------------------------------------
    op.create_table(
        "secrets",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "rotation_needed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_secrets_workspace_id",
        ),
        sa.UniqueConstraint("workspace_id", "name", name="uq_secrets_ws_name"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="valid_secret_status",
        ),
    )

    # --- secret_versions -------------------------------------------------------
    op.create_table(
        "secret_versions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("secret_id", UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=True),
        sa.Column("blob_ref", sa.String(length=512), nullable=True),
        sa.Column("alg", sa.String(length=16), nullable=False, server_default="age"),
        sa.Column(
            "recipients_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["secret_id"],
            ["secrets.id"],
            ondelete="CASCADE",
            name="fk_secret_versions_secret_id",
        ),
        sa.UniqueConstraint("secret_id", "version_number", name="uq_secret_versions_secret_ver"),
        sa.CheckConstraint("alg IN ('age')", name="valid_secret_version_alg"),
        sa.CheckConstraint(
            "(ciphertext IS NOT NULL) <> (blob_ref IS NOT NULL)",
            name="secret_version_one_storage",
        ),
    )

    # --- secret_grants ---------------------------------------------------------
    op.create_table(
        "secret_grants",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("secret_id", UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_pubkey_id", UUID(as_uuid=True), nullable=False),
        sa.Column("granted_by", sa.String(length=255), nullable=False),
        sa.Column("granted_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["secret_id"],
            ["secrets.id"],
            ondelete="CASCADE",
            name="fk_secret_grants_secret_id",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_pubkey_id"],
            ["recipient_pubkeys.id"],
            ondelete="CASCADE",
            name="fk_secret_grants_recipient_pubkey_id",
        ),
        sa.UniqueConstraint(
            "secret_id", "recipient_pubkey_id", name="uq_secret_grants_secret_pubkey"
        ),
    )
    op.create_index(
        "ix_secret_grants_recipient_pubkey_id",
        "secret_grants",
        ["recipient_pubkey_id"],
    )

    # --- secret_access_log (append-only, hash-chained) -------------------------
    op.create_table(
        "secret_access_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("secret_id", UUID(as_uuid=True), nullable=True),
        sa.Column("version_id", UUID(as_uuid=True), nullable=True),
        sa.Column("recipient_identity", sa.String(length=255), nullable=True),
        sa.Column("actor_user_id", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("req_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "prev_hash", name="uq_secret_access_log_ws_prev"),
        sa.CheckConstraint(
            "action IN ('register', 'approve', 'put', 'get', 'revoke')",
            name="valid_secret_access_log_action",
        ),
        sa.CheckConstraint(
            "result IN ('ok', 'denied', 'error')",
            name="valid_secret_access_log_result",
        ),
    )
    op.create_index("ix_secret_access_log_ws_id", "secret_access_log", ["workspace_id", "id"])
    op.create_index("ix_secret_access_log_secret_id", "secret_access_log", ["secret_id"])

    # DB-level append-only enforcement (#1128 gate1 finding 1). A row-level guard
    # blocks in-band UPDATE/DELETE, and a statement-level guard blocks TRUNCATE —
    # otherwise a TRUNCATE would silently reset the per-workspace hash chain back
    # to genesis with no forensic residue. Both reuse one guard function (TG_OP
    # names the blocked op). The chain catches any out-of-band mutation that
    # bypasses the triggers; an offsite checkpoint of each workspace's head hash
    # is the defense against a DDL-privileged DROP of the triggers themselves.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION secret_access_log_no_mutate()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION
                    'secret_access_log is append-only; % is not permitted', TG_OP
                    USING ERRCODE = 'restrict_violation';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER secret_access_log_append_only
            BEFORE UPDATE OR DELETE ON secret_access_log
            FOR EACH ROW EXECUTE FUNCTION secret_access_log_no_mutate();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER secret_access_log_no_truncate
            BEFORE TRUNCATE ON secret_access_log
            FOR EACH STATEMENT EXECUTE FUNCTION secret_access_log_no_mutate();
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS secret_access_log_no_truncate ON secret_access_log"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS secret_access_log_append_only ON secret_access_log"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS secret_access_log_no_mutate()"))
    op.drop_index("ix_secret_access_log_secret_id", table_name="secret_access_log")
    op.drop_index("ix_secret_access_log_ws_id", table_name="secret_access_log")
    op.drop_table("secret_access_log")
    op.drop_index("ix_secret_grants_recipient_pubkey_id", table_name="secret_grants")
    op.drop_table("secret_grants")
    op.drop_table("secret_versions")
    op.drop_table("secrets")
    op.drop_index("ix_recipient_pubkeys_fingerprint", table_name="recipient_pubkeys")
    op.drop_index("ix_recipient_pubkeys_ws_identity", table_name="recipient_pubkeys")
    op.drop_table("recipient_pubkeys")
