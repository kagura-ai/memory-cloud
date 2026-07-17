"""SQLAlchemy models for Resource management.

Issue #238 — Public Context: Resource-driven incremental indexing.

Provides ORM models for:

    - ``resources`` — authoritative resource entity (Issue #323)
    - ``resource_events`` — append-only event log
    - ``resource_schemas`` — Schema Registry
    - ``indexer_state`` — job tracking
    - ``resource_tokens`` — Resource API authentication
    - ``workspace_addons`` — Addon purchase system
    - ``workspace_connectors`` — ai-worker chat-ingest connector profiles
      (Issue #850, F6-a of #755; 1:1 with ``resources``)

Phase 1 (Issue #323, shipped v0.12.0): ``resource_pk`` and
``resource_tokens.workspace_id`` introduced as nullable shadow columns
populated by migration ``a97_resources_entity`` on existing rows.

Phase 2 (Issue #390, v0.12.3): all application writers now populate
``resource_pk`` on insert, and the ``before_insert`` event listener at
the bottom of this module enforces the invariant at the ORM layer —
inserts with ``resource_id`` set but ``resource_pk`` NULL raise
``IntegrityError``. Orphan backfill migration ``b01_resource_pk_ph2``
(file: ``b01_resource_pk_writer_phase2.py``) closes any rows written
between a97 and the writer migration, and includes a cross-workspace
slug ambiguity audit that aborts on the rare soft-delete-plus-reuse
shape the CWE-639 fix is meant to close.

Phase C (Issue #325, v0.13.0 follow-up): after a prod observation
window confirms no new NULL rows, ``resource_pk`` (and
``resource_tokens.workspace_id``) are tightened to NOT NULL, the
partial UNIQUE indexes are promoted to full UNIQUE, and a matching
PostgreSQL CHECK constraint is added. The app-layer listener becomes
redundant at that point but stays in place as defense-in-depth.

Dual-write prohibition: application code MUST NOT write
``resource_id`` independently of ``resource_pk``. The legacy
``resource_id`` column is a read-only mirror and will be dropped in a
Phase C+ cleanup once all external API contracts have migrated to
UUID-based identifiers.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,  # Issue #262: For importance column
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.constraint_names import RESOURCE_EVENTS_UPSERT_UNIQUE


class Resource(Base):
    """Authoritative resource entity (Issue #323).

    Serves as the single source of truth for workspace-scoped resource
    identity. Satellite tables (events, schemas, indexer_state, tokens)
    reference ``resources.id`` via their ``resource_pk`` FK column. The
    external-facing ``resource_id`` slug is kept here for API contracts
    (REST + MCP accept slug on input), but every internal relationship
    travels through the UUID primary key.

    Attributes:
        id: Primary key (UUID)
        workspace_id: Owning workspace (FK, CASCADE on workspace delete)
        resource_id: External-facing slug (unique within workspace)
        name: Human-readable label (populated by setup_resource;
            nullable because migration ``a97`` cannot infer a label from
            the ``contexts`` backfill source)
        created_by: User ID who created the resource (nullable for the
            same reason as ``name``)
        created_at: Creation timestamp
    """

    __tablename__ = "resources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "resource_id",
            name="uq_resources_workspace_resource_id",
        ),
    )


class ResourceEvent(Base):
    """Resource event model (append-only event log).

    Issue #238: Stores upsert/delete events from external systems (EC inventory, etc.).
    Issue #323: Added ``resource_pk`` FK to ``resources.id``. See module
    docstring for the dual-write prohibition rule.

    Attributes:
        id: Sequential ID for incremental reads (offset-based)
        resource_pk: Authoritative FK to ``resources.id`` (Issue #323)
        resource_id: Resource slug (legacy, read-only mirror of
            ``resources.resource_id`` — will be dropped in a follow-up)
        op: Operation type ('upsert' or 'delete')
        doc_id: Document identifier (stable across versions)
        version: Document version number (monotonically increasing)
        payload: Document payload as JSONB (NULL for delete)
        idempotency_key: Optional client-provided deduplication key
        created_at: Event creation timestamp
        event_metadata: Additional metadata (source, tenant, correlation_id, etc.)
    """

    __tablename__ = "resource_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    resource_pk: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    op: Mapped[str] = mapped_column(String(10), nullable=False)  # 'upsert' or 'delete'
    doc_id: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Issue #262: NULL = delete all versions
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # NULL for delete operations
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=True
    )
    # Issue #262: Importance for Memory creation
    # P2-8: NOT NULL with DEFAULT 0.6 (Migration 059)
    importance: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.6, server_default="0.6"
    )

    __table_args__ = (
        CheckConstraint("op IN ('upsert', 'delete')", name="check_op_type"),
        # Issue #323: partial UNIQUE index on (resource_pk, doc_id, version)
        # WHERE op = 'upsert' — delete events stay replayable so
        # upsert → delete → upsert revival works. Created via CREATE INDEX
        # CONCURRENTLY in migration a97_resources_entity; the declarative
        # Index keeps alembic autogenerate from reporting spurious drift.
        Index(
            RESOURCE_EVENTS_UPSERT_UNIQUE,
            "resource_pk",
            "doc_id",
            "version",
            unique=True,
            postgresql_where=text("op = 'upsert' AND resource_pk IS NOT NULL"),
        ),
    )


class ResourceSchema(Base):
    """Resource schema model (Schema Registry).

    Issue #238: Defines field metadata for JSONB payload interpretation.
    Issue #323: Added ``resource_pk`` FK to ``resources.id``. See module
    docstring for the dual-write prohibition rule.

    Attributes:
        id: Primary key
        resource_pk: Authoritative FK to ``resources.id`` (Issue #323)
        resource_id: Resource slug (legacy mirror — will be dropped)
        schema_version: Schema version number (monotonically increasing)
        field_definitions: JSONB array of field metadata
        created_at: Schema creation timestamp
        updated_at: Last modification timestamp
    """

    __tablename__ = "resource_schemas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_pk: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    field_definitions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Issue #323: partial UNIQUE(resource_pk, schema_version) created
        # by migration a97_resources_entity. Partial on
        # ``resource_pk IS NOT NULL`` during Phase 1 so rows awaiting
        # writer migration do not collide.
        Index(
            "uq_resource_schemas_version",
            "resource_pk",
            "schema_version",
            unique=True,
            postgresql_where=text("resource_pk IS NOT NULL"),
        ),
    )


class IndexerState(Base):
    """Indexer state model (job tracking).

    Issue #238: Tracks incremental indexer progress per (resource_id, context_id).
    Issue #323: Added ``resource_pk`` FK to ``resources.id``. See module
    docstring for the dual-write prohibition rule.

    Attributes:
        id: Primary key
        resource_pk: Authoritative FK to ``resources.id`` (Issue #323)
        resource_id: Resource slug (legacy mirror — will be dropped)
        context_id: Public context ID (foreign key to contexts table)
        last_offset: Last processed resource_events.id
        last_run_at: Last indexer execution timestamp
        next_run_at: Scheduled next run timestamp
        active_version: Active Qdrant collection version (for blue/green)
        job_status: Job status ('idle', 'queued', 'running', 'failed')
        metrics: Job execution metrics as JSONB
        created_at: State creation timestamp
        updated_at: Last modification timestamp
    """

    __tablename__ = "indexer_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_pk: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    last_offset: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    active_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    job_status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")
    metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "job_status IN ('idle', 'queued', 'running', 'failed')", name="check_job_status"
        ),
        # Issue #323: partial UNIQUE(resource_pk, context_id) created by
        # migration a97_resources_entity. Partial on
        # ``resource_pk IS NOT NULL`` during Phase 1 so rows awaiting
        # writer migration do not collide.
        Index(
            "uq_indexer_state_resource_context",
            "resource_pk",
            "context_id",
            unique=True,
            postgresql_where=text("resource_pk IS NOT NULL"),
        ),
    )


class ResourceToken(Base):
    """Resource token model (Resource API authentication).

    Issue #238: API tokens scoped to specific resource_id for secure external access.
    Issue #323: Added ``resource_pk`` FK to ``resources.id`` and
    ``workspace_id`` FK to ``workspaces.id`` as Phase 1 shadow columns.
    Once all writers populate them (epic #321, follow-up #324), #325
    tightens both to NOT NULL, at which point tenancy is enforced at
    the schema layer. Until then, authorization must continue to go
    through the legacy ``contexts`` JOIN path. See module docstring
    for the dual-write rule.

    Attributes:
        id: Primary key
        resource_pk: FK to ``resources.id`` (Issue #323, Phase 1 nullable)
        resource_id: Resource slug (legacy mirror — will be dropped)
        workspace_id: Owning workspace FK (Issue #323, Phase 1 nullable)
        token_hash: SHA256 hash of API token (never store plaintext)
        description: Human-readable description
        quota_events_per_hour: Max events allowed per hour
        created_by: User ID or system that created this token
        created_at: Token creation timestamp
        last_used_at: Last authentication timestamp
        is_active: Whether token is active (revocation support)
    """

    __tablename__ = "resource_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_pk: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quota_events_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WorkspaceAddon(Base):
    """Workspace addon model (Addon purchase system).

    Issue #238: Tracks addon purchases for variable quotas.
    Issue #665: ``source`` discriminator added so the admin grant path
    and the (future) Stripe webhook path can share this table as a
    single source of truth without overwriting each other.

    Attributes:
        id: Primary key
        workspace_id: Workspace UUID (foreign key)
        addon_type: Type of addon (extra_storage, extra_memory, etc.)
        quantity: Number of addon units purchased
        purchase_price_cents: Purchase price in cents
        stripe_product_id: Stripe product/price ID (optional)
        source: Provenance discriminator (``'stripe'`` for purchase
            flow, ``'admin_grant'`` for manual admin grants). The
            composite UNIQUE ``(workspace_id, addon_type, source)``
            enforces "at most one row per provenance per addon type",
            which is what makes the admin handler's UPSERT semantics
            safe (Issue #665, LD-2).
        active_from: Addon activation timestamp
        active_until: Addon expiration timestamp (NULL = permanent)
        created_at: Purchase timestamp
        created_by: User ID who purchased this addon
    """

    __tablename__ = "workspace_addons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    addon_type: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    purchase_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stripe_product_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="stripe", server_default="stripe"
    )
    active_from: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    active_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # NULL = permanent/subscription
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota', 'extra_rest_quota', 'extra_public_quota', 'extra_members', 'extra_contexts', 'extra_analysis_runs', 'extra_sleep_contexts', 'extra_connectors')",
            name="check_addon_type",
        ),
        CheckConstraint("quantity > 0", name="check_quantity_positive"),
        CheckConstraint(
            "source IN ('stripe', 'admin_grant')",
            name="check_addon_source",
        ),
        UniqueConstraint(
            "workspace_id",
            "addon_type",
            "source",
            name="uq_workspace_addons_workspace_addon_source",
        ),
    )


class WorkspaceConnector(Base):
    """ai-worker chat-ingest connector profile (Issue #850, F6-a of #755).

    1:1 with a ``resources`` row (``resource_pk`` UNIQUE) — each connector
    owns exactly one resource into which it ingests chat events. Net-new
    table for the F6 epic (#755); the setup flow, seat-cap enforcement, and
    connector-scoped token minting land in F6-b. This slice is schema only.

    Unlike the other Resource Foundation satellites (events / schemas /
    indexer_state / tokens), this table links to its resource purely by the
    ``resource_pk`` UUID FK and carries NO ``resource_id`` slug mirror, so it
    is intentionally **not** hooked into ``_enforce_resource_pk_invariant``
    (that listener guards the dual-write ``resource_id``-without-``resource_pk``
    shape). ``resource_pk`` is NOT NULL here, so the FK is always populated —
    and the absence of a slug sidesteps the CWE-639 slug-reuse class entirely.

    OAuth tokens are stored Fernet-encrypted in ``oauth_tokens_encrypted``;
    use :meth:`set_oauth_tokens` / :meth:`get_oauth_tokens` so plaintext never
    touches the column or the ORM identity map. See ``utils/encryption.py``.

    Attributes:
        id: Primary key (UUID)
        resource_pk: 1:1 FK to ``resources.id`` (UNIQUE, CASCADE)
        workspace_id: Owning workspace FK (CASCADE); denormalized for filter
            parity with the other resource tables — MUST stay consistent with
            ``resources.workspace_id``
        connector_type: One of ``slack`` / ``discord`` / ``teams``
        oauth_tokens_encrypted: Fernet ciphertext of the connector's OAuth
            token bundle (NULL until the F6-b setup flow populates it)
        pii_guardrail_config: PII-scrubbing config consumed by the ai-worker
            pre-compile stage (F6-d)
        runtime_config: Validated non-secret per-connector worker controls
        litellm_virtual_key_id: LiteLLM virtual-key identifier (NULL until set)
        config_version: Monotonic connector-config revision
        virtual_key_valid_until: Expiry of the LiteLLM virtual key (NULL = none)
        created_by: User ID who provisioned the connector
        created_at / updated_at: Lifecycle timestamps (naive UTC, per backend.md)
    """

    __tablename__ = "workspace_connectors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    resource_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connector_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Spec 2026-06-02: write-target context for the worker (path a). NULL on
    # legacy rows provisioned before the connector-registration refactor.
    context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Worker-facing pre-compile locale (e.g. cluster labelling). Defaults to the
    # workspace locale at provision time; NULL falls back to worker default.
    locale: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Stable platform app selector. Existing connectors are backfilled to the
    # compatibility identity ``default`` by migration e68 (#1315).
    app_key: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", server_default="default"
    )
    # Queryable external account/team id (Slack team_id / Discord guild / Teams
    # tenant). Dispatch is unique by (connector_type, app_key, this), allowing
    # the same platform team to install two distinct apps safely.
    external_team_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Ingest channel selection (Slack channel id list, v1 = ids only).
    channel_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    oauth_tokens_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fernet ciphertext of the BYO LLM config bundle ({provider, model, api_key}).
    llm_config_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fernet ciphertext of the workspace-scoped KMC write key (path a). The
    # api_keys row stores only the SHA256 hash for verification; this column
    # holds the encrypted plaintext so the worker config endpoint can hand it
    # back on every config fetch.
    kmc_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Expiry of the KMC write key. NULL = non-expiring (legacy). Set by the
    # rotate endpoint; the worker config endpoint logs a warning when expired.
    kmc_api_key_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Fernet ciphertext of the connector's resource token (X-Resource-API-Key,
    # #895). resource_tokens stores only the SHA256 hash, so the one-time
    # plaintext is captured here at provision time for the worker config
    # endpoint to return (resource-ingest write path, worker #91 Option A).
    # NULL on legacy rows → the worker falls back to the kmc/remember path.
    resource_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    pii_guardrail_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Optional for rolling compatibility: NULL rows predate #1348 and cause the
    # worker endpoint to omit the additive runtime block, preserving worker
    # defaults. Admin writes are validated and normalized before storage.
    runtime_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    litellm_virtual_key_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    config_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    virtual_key_valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # 1:1 connector -> resource. UNIQUE on the FK column is what enforces it.
        UniqueConstraint("resource_pk", name="uq_workspace_connectors_resource_pk"),
        CheckConstraint(
            "connector_type IN ('slack', 'discord', 'teams')",
            name="check_connector_type",
        ),
        # UNIQUE so one app-qualified platform team maps to exactly one
        # connector (prevents cross-tenant dispatch hijack). Postgres allows
        # multiple NULL external_team_id rows for not-yet-OAuthed connectors.
        Index(
            "ix_workspace_connectors_app_team",
            "connector_type",
            "app_key",
            "external_team_id",
            unique=True,
        ),
    )

    def set_oauth_tokens(self, tokens: dict[str, Any] | None) -> None:
        """Encrypt and store the OAuth token bundle — no plaintext persisted.

        Serializes ``tokens`` to JSON and Fernet-encrypts it into
        ``oauth_tokens_encrypted``. A falsy bundle clears the column (stores
        ``None``) rather than encrypting an empty string, which
        ``APIKeyEncryption.encrypt`` rejects.
        """
        import json

        from utils.encryption import get_encryptor

        if not tokens:
            self.oauth_tokens_encrypted = None
            return
        self.oauth_tokens_encrypted = get_encryptor().encrypt(json.dumps(tokens))

    def get_oauth_tokens(self) -> dict[str, Any] | None:
        """Decrypt and return the OAuth token bundle, or ``None`` if unset."""
        import json

        from utils.encryption import get_encryptor

        if not self.oauth_tokens_encrypted:
            return None
        return json.loads(get_encryptor().decrypt(self.oauth_tokens_encrypted))

    def set_llm_config(self, config: dict[str, Any] | None) -> None:
        """Encrypt and store the BYO LLM config bundle — no plaintext persisted."""
        import json

        from utils.encryption import get_encryptor

        if not config:
            self.llm_config_encrypted = None
            return
        self.llm_config_encrypted = get_encryptor().encrypt(json.dumps(config))

    def get_llm_config(self) -> dict[str, Any] | None:
        """Decrypt and return the BYO LLM config bundle, or ``None`` if unset."""
        import json

        from utils.encryption import get_encryptor

        if not self.llm_config_encrypted:
            return None
        return json.loads(get_encryptor().decrypt(self.llm_config_encrypted))

    def set_kmc_api_key(self, plaintext: str | None) -> None:
        """Encrypt and store the workspace-scoped KMC write key (path a)."""
        from utils.encryption import get_encryptor

        if not plaintext:
            self.kmc_api_key_encrypted = None
            return
        self.kmc_api_key_encrypted = get_encryptor().encrypt(plaintext)

    def get_kmc_api_key(self) -> str | None:
        """Decrypt and return the KMC write key plaintext, or ``None`` if unset."""
        from utils.encryption import get_encryptor

        if not self.kmc_api_key_encrypted:
            return None
        return get_encryptor().decrypt(self.kmc_api_key_encrypted)

    def set_resource_token(self, plaintext: str | None) -> None:
        """Encrypt and store the connector's resource token (X-Resource-API-Key, #895)."""
        from utils.encryption import get_encryptor

        if not plaintext:
            self.resource_token_encrypted = None
            return
        self.resource_token_encrypted = get_encryptor().encrypt(plaintext)

    def get_resource_token(self) -> str | None:
        """Decrypt and return the resource token plaintext, or ``None`` if unset."""
        from utils.encryption import get_encryptor

        if not self.resource_token_encrypted:
            return None
        return get_encryptor().decrypt(self.resource_token_encrypted)


# ---------------------------------------------------------------------------
# Writer invariant (Issue #390 — Phase 2)
# ---------------------------------------------------------------------------
# Application-layer enforcement of the dual-write prohibition spelled out in
# this module's top docstring: once Phase 2 writers are migrated, any INSERT
# that sets ``resource_id`` without also setting ``resource_pk`` is a bug.
# Catching it here prevents future writer paths from silently producing
# orphan rows that reintroduce the CWE-639 slug-reuse leak. The DB-level
# CHECK constraint equivalent is intentionally deferred to Phase C (#325) so
# the prod observation window (``resource_pk IS NULL`` row count should
# drain to zero over one week) is not invalidated by a hard schema gate.


def _enforce_resource_pk_invariant(mapper, connection, target) -> None:
    """Raise if ``resource_id`` is populated without a matching ``resource_pk``.

    Hooked into ``before_insert`` for every satellite model. UPDATE paths
    are NOT hooked — a single-column ``resource_pk`` clearing is unreachable
    through normal ORM use (FK column, not user-settable in any route), and
    adding ``before_update`` would fire on every unrelated column write with
    no load-bearing invariant to check.
    """
    del mapper, connection  # SQLAlchemy event contract — we only inspect target.
    if target.resource_id is not None and target.resource_pk is None:
        raise IntegrityError(
            statement=None,
            params=None,
            orig=ValueError(
                f"{type(target).__name__}: resource_pk must be populated "
                f"when resource_id is set (resource_id={target.resource_id!r}). "
                "See models/resource.py top docstring for the Phase 2 writer "
                "contract."
            ),
        )


for _model in (ResourceEvent, ResourceSchema, IndexerState, ResourceToken):
    event.listen(_model, "before_insert", _enforce_resource_pk_invariant)
