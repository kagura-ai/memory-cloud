"""Zero-knowledge secret store models (Issue #1128).

A **passive, ciphertext-only** store for devops API keys (Cloudflare / Resend /
GCP / …). The trust model is zero-knowledge: each engineer/agent holds an
``age`` (X25519) **private** key locally and registers only the **public**
recipient key here. memory-cloud stores public keys + ciphertext only and **can
never decrypt** a secret — so a full server compromise leaks ciphertext the
attacker cannot read, and secrets never touch the LLM / recall surface.

This is the SERVER side. ``age`` encryption/decryption and the ``kagura secret``
CLI live in ``kagura-ai/kagura-memory-python-sdk``. The server validates the
*shape* of a recipient key but performs no cryptography on secret values.

Isolation (hard requirement, sibling to #210): these tables are their own models
with their own ``__tablename__`` and **no ``embedding_status`` column**, so they
are structurally invisible to the embedding sweep and ``recall()`` — both of
which are bound to ``models.memory.Memory`` (the ``memories`` table). A stored
secret can therefore never appear in a recall result nor be enqueued for
embedding; ``backend/tests`` asserts this in both directions.

Why a dedicated schema rather than the existing credential paths: ``ExternalAPIKey``
+ ``utils.encryption`` (Fernet) and ``MemberCredentialsService`` are
**server-custody** — the server holds ``API_KEY_SECRET`` and can decrypt. This
feature deliberately does NOT reuse them; the server holds no key for these
ciphertexts. The dedicated-table choice mirrors ``ShareKey`` (#1027): security
invariants are made *structural*, not policy.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# --- Recipient pubkey lifecycle ------------------------------------------------
# A registered pubkey starts ``pending`` (TOFU trust root, #1128 gate1) and an
# owner must ``approve`` it (→ ``active``) before any grant can target it. Owner
# revocation moves it to ``revoked``. New states are appended, never reordered.
PUBKEY_STATUS_PENDING = "pending"
PUBKEY_STATUS_ACTIVE = "active"
PUBKEY_STATUS_REVOKED = "revoked"
_ALL_PUBKEY_STATUSES: tuple[str, ...] = (
    PUBKEY_STATUS_PENDING,
    PUBKEY_STATUS_ACTIVE,
    PUBKEY_STATUS_REVOKED,
)

# --- Secret lifecycle ----------------------------------------------------------
SECRET_STATUS_ACTIVE = "active"
SECRET_STATUS_DISABLED = "disabled"
_ALL_SECRET_STATUSES: tuple[str, ...] = (
    SECRET_STATUS_ACTIVE,
    SECRET_STATUS_DISABLED,
)

# --- Ciphertext algorithm ------------------------------------------------------
# Only ``age`` is supported. The column exists so a future alg can be added
# without a schema change, but the server never interprets the ciphertext.
SECRET_ALG_AGE = "age"
_ALL_SECRET_ALGS: tuple[str, ...] = (SECRET_ALG_AGE,)

# --- Audit log enums -----------------------------------------------------------
AUDIT_ACTION_REGISTER = "register"
AUDIT_ACTION_APPROVE = "approve"
AUDIT_ACTION_PUT = "put"
AUDIT_ACTION_GET = "get"
AUDIT_ACTION_REVOKE = "revoke"
_ALL_AUDIT_ACTIONS: tuple[str, ...] = (
    AUDIT_ACTION_REGISTER,
    AUDIT_ACTION_APPROVE,
    AUDIT_ACTION_PUT,
    AUDIT_ACTION_GET,
    AUDIT_ACTION_REVOKE,
)

AUDIT_RESULT_OK = "ok"
AUDIT_RESULT_DENIED = "denied"
AUDIT_RESULT_ERROR = "error"
_ALL_AUDIT_RESULTS: tuple[str, ...] = (
    AUDIT_RESULT_OK,
    AUDIT_RESULT_DENIED,
    AUDIT_RESULT_ERROR,
)

# Genesis link for the first audit entry in a workspace's hash chain.
AUDIT_GENESIS_HASH = "0" * 64

# Field caps (single source of truth — enforced at API schema + service layer).
SECRET_NAME_MAX_LEN = 255
PUBKEY_MAX_LEN = 128
PUBKEY_LABEL_MAX_LEN = 100
IDENTITY_MAX_LEN = 255


class RecipientPubkey(Base):
    """An ``age`` recipient (public) key bound to a workspace identity.

    The server stores the public key and a SHA256 ``fingerprint`` of it; it
    never holds the matching private key. ``status`` gates use: only an
    ``active`` (owner-approved) pubkey may be a grant target.
    """

    __tablename__ = "recipient_pubkeys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        # FK lookups are served by the leading column of ix_recipient_pubkeys_ws_identity.
    )
    # The identity (user/agent id) that owns the matching private key.
    identity_id: Mapped[str] = mapped_column(String(IDENTITY_MAX_LEN), nullable=False)
    # The ``age`` recipient public key (e.g. ``age1qz...``). Opaque to the server.
    pubkey: Mapped[str] = mapped_column(String(PUBKEY_MAX_LEN), nullable=False)
    # SHA256 hex of the pubkey — stable handle used in recipients_snapshot and
    # audit metadata so the (longer) pubkey need not be echoed everywhere.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(PUBKEY_LABEL_MAX_LEN), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PUBKEY_STATUS_PENDING,
        server_default=PUBKEY_STATUS_PENDING,
    )
    created_by: Mapped[str] = mapped_column(String(IDENTITY_MAX_LEN), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    # Set when an owner approves the pending registration (TOFU attestation).
    attested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attested_by: Mapped[str | None] = mapped_column(String(IDENTITY_MAX_LEN), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # One row per (workspace, pubkey): the same recipient key cannot be
        # registered twice in a workspace.
        UniqueConstraint("workspace_id", "pubkey", name="uq_recipient_pubkeys_ws_pubkey"),
        Index("ix_recipient_pubkeys_ws_identity", "workspace_id", "identity_id"),
        Index("ix_recipient_pubkeys_fingerprint", "fingerprint"),
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _ALL_PUBKEY_STATUSES)})",
            name="valid_recipient_pubkey_status",
        ),
    )

    def __repr__(self) -> str:
        return f"<RecipientPubkey(identity='{self.identity_id}', fp='{self.fingerprint[:12]}')>"


class Secret(Base):
    """A named secret. Holds metadata only; ciphertext lives in versions."""

    __tablename__ = "secrets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        # FK lookups are served by the leading column of uq_secrets_ws_name.
    )
    # Logical name, e.g. ``cloudflare/api-token``. Unique per workspace.
    name: Mapped[str] = mapped_column(String(SECRET_NAME_MAX_LEN), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SECRET_STATUS_ACTIVE,
        server_default=SECRET_STATUS_ACTIVE,
    )
    # Set true on grant revocation: the stored ciphertext is still readable by
    # holders who already fetched it, so revoke means "rotate upstream", not
    # "un-share" (#1128 gate1 finding 4). Cleared when a new version is put.
    rotation_needed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_by: Mapped[str] = mapped_column(String(IDENTITY_MAX_LEN), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_secrets_ws_name"),
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _ALL_SECRET_STATUSES)})",
            name="valid_secret_status",
        ),
    )

    def __repr__(self) -> str:
        return f"<Secret(name='{self.name}')>"


class SecretVersion(Base):
    """One immutable ciphertext version of a secret.

    ``recipients_snapshot`` is the list of pubkey fingerprints the ciphertext
    was encrypted to at put time — the server checks it equals the granted,
    active recipient set (grant ⇄ ciphertext integrity, #1128 gate1 finding 6).
    """

    __tablename__ = "secret_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    secret_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secrets.id", ondelete="CASCADE"),
        nullable=False,
        # FK lookups are served by the leading column of uq_secret_versions_secret_ver.
    )
    # Monotonic per secret (1, 2, 3, …). The "current" version is max(version_number).
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Inline armored ``age`` ciphertext. Opaque; the server never decrypts it.
    ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Future: large ciphertext offloaded to R2; one of ciphertext/blob_ref is set.
    blob_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    alg: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SECRET_ALG_AGE, server_default=SECRET_ALG_AGE
    )
    # list[str] of recipient fingerprints (sha256 hex of each pubkey).
    recipients_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String(IDENTITY_MAX_LEN), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("secret_id", "version_number", name="uq_secret_versions_secret_ver"),
        CheckConstraint(
            f"alg IN ({', '.join(repr(a) for a in _ALL_SECRET_ALGS)})",
            name="valid_secret_version_alg",
        ),
        # Exactly-one-of storage: inline ciphertext or an R2 blob ref.
        CheckConstraint(
            "(ciphertext IS NOT NULL) <> (blob_ref IS NOT NULL)",
            name="secret_version_one_storage",
        ),
    )

    def __repr__(self) -> str:
        return f"<SecretVersion(secret_id='{self.secret_id}', v={self.version_number})>"


class SecretGrant(Base):
    """An access grant: ``recipient_pubkey_id`` may fetch ``secret_id``.

    Default-deny: a fetch is allowed only when an active (non-revoked) grant
    links the secret to one of the caller's active recipient pubkeys.
    """

    __tablename__ = "secret_grants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    secret_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secrets.id", ondelete="CASCADE"),
        nullable=False,
        # FK lookups are served by the leading column of uq_secret_grants_secret_pubkey.
    )
    recipient_pubkey_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recipient_pubkeys.id", ondelete="CASCADE"),
        nullable=False,
    )
    granted_by: Mapped[str] = mapped_column(String(IDENTITY_MAX_LEN), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("secret_id", "recipient_pubkey_id", name="uq_secret_grants_secret_pubkey"),
        # The unique constraint above leads with secret_id; this serves the
        # recipient_pubkey_id FK (cascade on pubkey delete) and reverse lookups.
        Index("ix_secret_grants_recipient_pubkey_id", "recipient_pubkey_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<SecretGrant(secret_id='{self.secret_id}', pubkey_id='{self.recipient_pubkey_id}')>"
        )


class SecretAccessLog(Base):
    """Append-only, tamper-evident audit of every secret-store action.

    Append-only is enforced at the **DB level** by a ``BEFORE UPDATE OR DELETE``
    trigger (see the migration) — application discipline alone is not trusted.
    Tamper-evidence is a per-workspace HMAC hash chain: ``entry_hash =
    HMAC(audit_hmac_key, prev_hash || canonical(entry))`` where ``prev_hash`` is
    the previous entry's ``entry_hash`` for the same workspace (``AUDIT_GENESIS_HASH``
    for the first). A row mutation breaks the chain and is independently detectable.

    The log is deliberately **decoupled** from the entities it audits: ``secret_id`` /
    ``version_id`` are plain columns with no FK, so the audit trail survives a
    secret delete and a secret can never be blocked from deletion by its history.
    Secret values are **never** written here — only names, ids, identities, and
    request metadata.
    """

    __tablename__ = "secret_access_log"

    # BigInteger cursor PK (matches resource_events / llm_call_log append-only logs).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # No FK — the audit row outlives the secret/version it references.
    secret_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # The recipient/identity the action concerned (e.g. the registered pubkey owner).
    recipient_identity: Mapped[str | None] = mapped_column(String(IDENTITY_MAX_LEN), nullable=True)
    # The authenticated caller that performed the action.
    actor_user_id: Mapped[str] = mapped_column(String(IDENTITY_MAX_LEN), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    # Non-secret request metadata (tool/source/ip/ua). NEVER the secret value.
    req_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Per-workspace chain reads (latest entry by id within a workspace).
        Index("ix_secret_access_log_ws_id", "workspace_id", "id"),
        Index("ix_secret_access_log_secret_id", "secret_id"),
        # Structural anti-fork guard: each prev_hash is used at most once per
        # workspace, so the chain is strictly linear (a second writer that read
        # the same tip cannot commit a sibling). Complements the advisory lock
        # the service takes while appending.
        UniqueConstraint("workspace_id", "prev_hash", name="uq_secret_access_log_ws_prev"),
        CheckConstraint(
            f"action IN ({', '.join(repr(a) for a in _ALL_AUDIT_ACTIONS)})",
            name="valid_secret_access_log_action",
        ),
        CheckConstraint(
            f"result IN ({', '.join(repr(r) for r in _ALL_AUDIT_RESULTS)})",
            name="valid_secret_access_log_result",
        ),
    )

    def __repr__(self) -> str:
        return f"<SecretAccessLog(id={self.id}, action='{self.action}', result='{self.result}')>"
