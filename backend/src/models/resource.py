"""SQLAlchemy models for Resource management.

Issue #238 — Public Context: Resource-driven incremental indexing.

Provides ORM models for:

    - ``resources`` — authoritative resource entity (Issue #323)
    - ``resource_events`` — append-only event log
    - ``resource_schemas`` — Schema Registry
    - ``indexer_state`` — job tracking
    - ``resource_tokens`` — Resource API authentication
    - ``workspace_addons`` — Addon purchase system

Phase 1 rollout (Issue #323): ``resource_pk`` and
``resource_tokens.workspace_id`` are nullable shadow columns populated
by migration ``a97_resources_entity`` on existing rows. Writers that
have not yet been updated (tracked under #324) still work because the
columns are nullable during Phase 1; once all writers populate them,
migration #325 tightens them to NOT NULL. Application code MUST NOT
write ``resource_id`` independently of ``resource_pk`` once the
writer migration begins — see the migration docstring for the full
rationale.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,  # Issue #262: For importance column
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

from db.base import Base


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

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id = Column(String(255), nullable=False, index=True)
    name = Column(Text, nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

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

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    resource_pk = Column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    resource_id = Column(String(255), nullable=False, index=True)
    op = Column(String(10), nullable=False)  # 'upsert' or 'delete'
    doc_id = Column(String(255), nullable=False)
    version = Column(Integer, nullable=True)  # Issue #262: NULL = delete all versions
    payload = Column(JSONB, nullable=True)  # NULL for delete operations
    idempotency_key = Column(String(255), unique=True, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    event_metadata = Column(JSONB, default={}, server_default="{}")
    # Issue #262: Importance for Memory creation
    # P2-8: NOT NULL with DEFAULT 0.6 (Migration 059)
    importance = Column(Float, nullable=False, default=0.6, server_default="0.6")

    __table_args__ = (
        CheckConstraint("op IN ('upsert', 'delete')", name="check_op_type"),
        # Issue #323: partial UNIQUE index on (resource_pk, doc_id, version)
        # WHERE op = 'upsert' — delete events stay replayable so
        # upsert → delete → upsert revival works. Created via CREATE INDEX
        # CONCURRENTLY in migration a97_resources_entity; the declarative
        # Index keeps alembic autogenerate from reporting spurious drift.
        Index(
            "ux_resource_events_upsert_version",
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

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_pk = Column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    resource_id = Column(String(255), nullable=False, index=True)
    schema_version = Column(Integer, nullable=False, default=1)
    field_definitions = Column(JSONB, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

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

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_pk = Column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    resource_id = Column(String(255), nullable=False, index=True)
    context_id = Column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    last_offset = Column(BigInteger, nullable=False, default=0)
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True, index=True)
    active_version = Column(Integer, nullable=False, default=1)
    job_status = Column(String(20), nullable=False, default="idle")
    metrics = Column(JSONB, default={}, server_default="{}")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

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

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_pk = Column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    resource_id = Column(String(255), nullable=False, index=True)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,  # Phase 1 shadow column — tightened in #325
        index=True,
    )
    token_hash = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    quota_events_per_hour = Column(Integer, nullable=False, default=1000)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    last_used_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)


class WorkspaceAddon(Base):
    """Workspace addon model (Addon purchase system).

    Issue #238: Tracks addon purchases for variable quotas.

    Attributes:
        id: Primary key
        workspace_id: Workspace UUID (foreign key)
        addon_type: Type of addon (extra_storage, extra_memory, etc.)
        quantity: Number of addon units purchased
        purchase_price_cents: Purchase price in cents
        stripe_product_id: Stripe product/price ID (optional)
        active_from: Addon activation timestamp
        active_until: Addon expiration timestamp (NULL = permanent)
        created_at: Purchase timestamp
        created_by: User ID who purchased this addon
    """

    __tablename__ = "workspace_addons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    addon_type = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    purchase_price_cents = Column(Integer, nullable=True)
    stripe_product_id = Column(String(100), nullable=True)
    active_from = Column(DateTime, nullable=False, server_default=func.now())
    active_until = Column(DateTime, nullable=True)  # NULL = permanent/subscription
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    created_by = Column(String(255), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota', 'extra_rest_quota', 'extra_public_quota', 'extra_members', 'extra_contexts')",
            name="check_addon_type",
        ),
        CheckConstraint("quantity > 0", name="check_quantity_positive"),
    )
