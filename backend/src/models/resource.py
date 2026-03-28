"""SQLAlchemy models for Resource management.

Issue #238 - Public Context: Resource-driven incremental indexing

Provides ORM models for:
- resource_events table (append-only event log)
- resource_schemas table (Schema Registry)
- indexer_state table (job tracking)
- resource_tokens table (Resource API authentication)
- workspace_addons table (Addon purchase system)
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,  # Issue #262: For importance column
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db.base import Base


class ResourceEvent(Base):
    """Resource event model (append-only event log).

    Issue #238: Stores upsert/delete events from external systems (EC inventory, etc.)

    Attributes:
        id: Sequential ID for incremental reads (offset-based)
        resource_id: Resource identifier (e.g., "ec_products")
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
        # Unique constraint prevents duplicate document versions
        # Handled in migration: CONSTRAINT unique_resource_doc_version UNIQUE (resource_id, doc_id, version)
    )


class ResourceSchema(Base):
    """Resource schema model (Schema Registry).

    Issue #238: Defines field metadata for JSONB payload interpretation.

    Attributes:
        id: Primary key
        resource_id: Resource identifier (matches resource_events.resource_id)
        schema_version: Schema version number (monotonically increasing)
        field_definitions: JSONB array of field metadata
        created_at: Schema creation timestamp
        updated_at: Last modification timestamp
    """

    __tablename__ = "resource_schemas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_id = Column(String(255), nullable=False, index=True)
    schema_version = Column(Integer, nullable=False, default=1)
    field_definitions = Column(JSONB, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # Unique constraint ensures one schema per version
        # Handled in migration: CONSTRAINT unique_resource_schema_version UNIQUE (resource_id, schema_version)
    )


class IndexerState(Base):
    """Indexer state model (job tracking).

    Issue #238: Tracks incremental indexer progress per (resource_id, context_id).

    Attributes:
        id: Primary key
        resource_id: Resource identifier
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
        # Unique constraint ensures one indexer per (resource, context) pair
        # Handled in migration: CONSTRAINT unique_resource_context UNIQUE (resource_id, context_id)
    )


class ResourceToken(Base):
    """Resource token model (Resource API authentication).

    Issue #238: API tokens scoped to specific resource_id for secure external access.

    Attributes:
        id: Primary key
        resource_id: Resource identifier this token is authorized for
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
    resource_id = Column(String(255), nullable=False, index=True)
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
            "addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota', 'extra_rest_quota', 'extra_public_quota', 'extra_members')",
            name="check_addon_type",
        ),
        CheckConstraint("quantity > 0", name="check_quantity_positive"),
    )
