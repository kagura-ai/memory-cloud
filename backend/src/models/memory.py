"""SQLAlchemy models for memory storage.

Based on Issue #1 - Kagura Memory Cloud backend specification.

Provides ORM models for:
- memories table (3-layer memory with Working/Persistent)
- graph_memory table (NetworkX graph JSON storage)
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,  # Migration 061: For generated columns
    DateTime,
    Float,
    ForeignKey,  # Migration 062: For context_id FK
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Memory(Base):
    """Memory model with 3-layer architecture.

    Issue #1 specification:
    - Layer 1: summary (Embedding化、検索用)
    - Layer 2: context_summary (文脈説明)
    - Layer 3: content + details (完全詳細)

    Working/Persistent memory with automatic promotion.

    Attributes:
        id: UUID primary key
        user_id: Owner user ID
        summary: Layer 1 - 検索用サマリー (50-200文字)
        summary_embedding_id: Qdrant point ID
        context_summary: Layer 2 - 文脈説明 (200-1000文字)
        content: Layer 3 - 基本内容
        details: Layer 3 - 完全詳細 (JSONB)
        type: メモリタイプ (code, note, decision, error etc.)
        importance: 重要度 (0.0-1.0)
        confidence: 信頼度 (0.0-1.0)
        tags: タグ配列
        context: コンテキスト情報 (JSONB)
        scope: working または persistent
        long_term: Long-term memory flag
        promoted_at: Promotion timestamp
        use_count: 使用回数
        last_used_at: 最終使用時刻
        accessed_by_clients: アクセスしたクライアント一覧
        access_count: アクセス回数
        created_at: 作成時刻
        updated_at: 更新時刻
        deleted_at: Soft delete timestamp (NULL = active)
        deleted_by: User who deleted this memory
        client: クライアント名
        client_version: クライアントバージョン
    """

    __tablename__ = "memories"

    # 基本情報
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Migration 063: 3-level isolation (workspace, context, user)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Layer 1: 検索用サマリー
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    summary_embedding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Issue #122: Embedding status tracking for transaction integrity
    # Values: 'pending', 'success', 'failed'
    embedding_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    embedding_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Layer 2: 文脈説明
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Layer 3: 完全詳細
    content: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # メタデータ（LLM判断）
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Working/Persistent メモリ
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="working", index=True)
    long_term: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 利用統計（Consolidation判定用）
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accessed_by_clients: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # システム情報
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )

    # Logical Delete (Issue #46 Phase 4)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # クライアント情報
    client: Mapped[str] = mapped_column(String(100), nullable=False)
    client_version: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Issue #262: Track memory creation source
    # P2-7: Remove index=True (index created in migration 057)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="mcp_remember")

    # Issue #213: Origin URI for external integration (Obsidian, code ingestion, web clipping)
    # Partial index idx_memories_source_uri created in migration a95
    source_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # source_type: file, url, vault, api, manual

    # Migration 061: Generated columns for efficient resource memory lookups
    # These columns are automatically computed from details JSONB field
    resource_id: Mapped[str | None] = mapped_column(
        String(255),
        Computed("details->>'resource_id'", persisted=True),
        index=False,  # Index created in Migration 061
    )
    resource_doc_id: Mapped[str | None] = mapped_column(
        String(255),
        Computed("details->>'doc_id'", persisted=True),
        index=False,  # Index created in Migration 061
    )
    resource_version: Mapped[int | None] = mapped_column(
        Integer,
        Computed(
            "CASE WHEN details->>'version' ~ '^[0-9]+$' THEN (details->>'version')::INTEGER ELSE NULL END",
            persisted=True,
        ),
        index=False,  # Index created in Migration 061
    )

    # Issue #485: Polymorphic blob reference inside details.external_blob.
    # Phase 1 only writes backend='platform_r2'; Phase 2 BYO adds
    # 'byo_s3'/'byo_gcs' rows without altering this schema.
    external_blob_backend: Mapped[str | None] = mapped_column(
        String(50),
        Computed("details->'external_blob'->>'backend'", persisted=True),
        index=False,  # Partial btree index created in migration e03_485
    )
    external_blob_ref: Mapped[str | None] = mapped_column(
        String(2048),
        Computed("details->'external_blob'->>'ref'", persisted=True),
        index=False,  # Partial btree index created in migration e03_485
    )

    # Constraints
    __table_args__ = (
        CheckConstraint("importance BETWEEN 0 AND 1", name="valid_importance"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="valid_confidence"),
        CheckConstraint("scope IN ('working', 'persistent')", name="valid_scope"),
        CheckConstraint(
            "embedding_status IN ('pending', 'processing', 'success', 'failed')",
            name="valid_embedding_status",
        ),
        # Indexes
        Index("idx_user_scope", "user_id", "scope"),
        Index("idx_user_type", "user_id", "type"),
        Index("idx_consolidation", "user_id", "long_term", "use_count", "importance"),
        Index("idx_created_at", "created_at"),
        Index("idx_last_used", "last_used_at"),
        # JSONB index for context_id
        Index("idx_context", func.cast(context["context_id"], String)),
    )

    def __repr__(self) -> str:
        return f"<Memory(id='{self.id}', type='{self.type}', scope='{self.scope}')>"


class Attachment(Base):
    """File attachment for memories.

    Issue #330: Store files in PostgreSQL BYTEA.
    Size limit: 5MB. Allowed types: image/png, image/jpeg, image/gif, application/pdf, text/plain.
    """

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("size_bytes > 0 AND size_bytes <= 5242880", name="attachment_size_limit"),
    )

    def __repr__(self) -> str:
        return f"<Attachment(id='{self.id}', filename='{self.filename}', size={self.size_bytes})>"


class GraphMemory(Base):
    """Graph memory model for NetworkX graph storage.

    Issue #1 specification:
    - Stores entire NetworkX graph as JSON (node_link_data format)
    - One graph per user
    - Used for Neural Memory and explore() API

    Attributes:
        id: Primary key
        user_id: Owner user ID (unique - one graph per user)
        graph_data: NetworkX graph as JSON
        total_nodes: Node count (cached)
        total_edges: Edge count (cached)
        avg_edge_weight: Average edge weight (cached)
        max_edge_weight: Max edge weight (cached)
        created_at: Creation timestamp
        updated_at: Last modification timestamp
        last_decay_at: Last decay operation timestamp
        last_consolidation_at: Last consolidation timestamp
    """

    __tablename__ = "graph_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)

    # NetworkX グラフ全体（node_link_data形式）
    graph_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # 統計情報（キャッシュ・監視用）
    total_nodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_edges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_edge_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_edge_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # タイムスタンプ
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # パフォーマンス監視
    last_decay_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_consolidation_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<GraphMemory(user='{self.user_id}', nodes={self.total_nodes}, edges={self.total_edges})>"


# Edge type constants — named aliases for the persisted edge_type string values.
# Python validators (#461 PR #507, #506 PR #508) reference these constants by name.
# The DB CHECK constraint is generated from the ordered ``_ALL_EDGE_TYPES`` tuple
# (see below), which enumerates the same values in registration order.
EDGE_TYPE_NEURAL_ASSOCIATION = "neural_association"
EDGE_TYPE_RELATED_TO = "related_to"
EDGE_TYPE_DEPENDS_ON = "depends_on"
EDGE_TYPE_LEARNED_FROM = "learned_from"
EDGE_TYPE_SEMANTIC_SIMILARITY = "semantic_similarity"
EDGE_TYPE_DECLARED_LINK = "declared_link"
EDGE_TYPE_TAG_COOCCURRENCE = "tag_cooccurrence"

# Issue #509 (Phase B of #461): registration-order tuple used to derive the
# ``valid_edge_type`` CHECK constraint string in ``NeuralMemoryEdge.__table_args__``.
# Order MUST match the literal order ``b05_223_tag_cooccurrence.py`` installed
# on prod (its ``_NEW_EDGE_TYPES_SQL``) so ``Base.metadata.create_all()`` produces
# a CHECK string byte-identical to alembic head — preventing
# ``alembic revision --autogenerate`` from generating a spurious no-op migration
# on future runs.
_ALL_EDGE_TYPES: tuple[str, ...] = (
    EDGE_TYPE_NEURAL_ASSOCIATION,
    EDGE_TYPE_RELATED_TO,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_LEARNED_FROM,
    EDGE_TYPE_SEMANTIC_SIMILARITY,
    EDGE_TYPE_DECLARED_LINK,
    EDGE_TYPE_TAG_COOCCURRENCE,
)

# Edge origin discriminator (Issue #722).
# 'hebbian' = runtime co-activation (HebbianLearner) — decays
# 'semantic' = sleep edge_discovery (cosine-similarity-derived) — does not decay
# 'declared' = user-asserted links — does not decay
EDGE_ORIGIN_HEBBIAN = "hebbian"
EDGE_ORIGIN_SEMANTIC = "semantic"
EDGE_ORIGIN_DECLARED = "declared"

_ALL_EDGE_ORIGINS: tuple[str, ...] = (
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    EDGE_ORIGIN_DECLARED,
)


class NeuralMemoryEdge(Base):
    """Neural Memory edge model (Issue #84 Phase 1).

    Replaces NetworkX JSONB storage with normalized PostgreSQL edge table
    for improved performance and scalability.

    Benefits:
        - 10x faster graph operations (SQL queries vs JSON serialization)
        - Better concurrency (row-level locking)
        - Efficient BFS traversal (recursive CTEs)
        - GDPR compliant (CASCADE delete)
    """

    __tablename__ = "neural_memory_edges"

    # Primary key
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Graph structure
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    src_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    dst_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)

    # Migration 062 added these as nullable; b03_396 backfilled NULL rows
    # from endpoint memories and enforced NOT NULL via CHECK constraint +
    # SET NOT NULL so the context_id FK's ON DELETE CASCADE cannot be
    # bypassed (prior NULL rows were a GDPR right-to-erasure death zone).
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Edge properties
    edge_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default=EDGE_TYPE_NEURAL_ASSOCIATION
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # Edge origin (Issue #722). Decay/prune skip origin != 'hebbian'.
    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EDGE_ORIGIN_HEBBIAN, server_default=EDGE_ORIGIN_HEBBIAN
    )

    # Metadata (flexible JSONB for future extensions)
    # Note: 'metadata' is reserved in SQLAlchemy, use edge_metadata
    edge_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Table constraints
    __table_args__ = (
        UniqueConstraint("user_id", "src_id", "dst_id", name="unique_edge"),
        CheckConstraint("weight >= 0.0 AND weight <= 3.0", name="valid_weight"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="valid_confidence"),
        # Issue #509 (Phase B of #461): CHECK constraint derived from
        # ``_ALL_EDGE_TYPES``. Adding a new edge_type requires THREE coordinated
        # edits (caught by the regression test if any are missed): (1) add
        # ``EDGE_TYPE_NEW`` to the constants block above, (2) append it to
        # ``_ALL_EDGE_TYPES``, (3) update the expected literal in
        # ``test_valid_edge_type_check_constraint_matches_migration_literal``,
        # plus a corresponding alembic migration that ALTERs the prod CHECK.
        # The f-string output is byte-identical to ``b05_223_tag_cooccurrence.py``'s
        # ``_NEW_EDGE_TYPES_SQL`` (registration order, single quotes, exact whitespace).
        CheckConstraint(
            f"edge_type IN ({', '.join(repr(t) for t in _ALL_EDGE_TYPES)})",
            name="valid_edge_type",
        ),
        # Mirrors the workspace_id/context_id NOT NULL CHECK that
        # b03_396_neural_edges_ws_ctx_not_null.py installed so
        # Base.metadata.create_all() (tests, fresh dev DB) produces the same
        # schema the migration path leaves production in. NOT NULL on the
        # columns already enforces this, so the CHECK is belt-and-braces — but
        # keeping it symmetric with the migration avoids "works on prod,
        # silently lax on test fixtures" drift.
        CheckConstraint(
            "workspace_id IS NOT NULL AND context_id IS NOT NULL",
            name="ck_neural_memory_edges_ws_ctx_not_null",
        ),
        # Drift between this CHECK and the e17_722 migration literal is pinned
        # by test_valid_edge_origin_check_constraint_matches_migration_literal.
        CheckConstraint(
            f"origin IN ({', '.join(repr(o) for o in _ALL_EDGE_ORIGINS)})",
            name="valid_edge_origin",
        ),
        Index("idx_edges_user_src", "user_id", "src_id"),
        Index("idx_edges_user_dst", "user_id", "dst_id"),
        # Issue #383: composite indexes matching the new PermissionService-driven
        # graph read path. ``(workspace_id, context_id)``-leading, so leftmost-
        # prefix matches ``WHERE workspace_id = :ws AND context_id = :ctx`` for
        # shared-context visualization without relying on ``user_id``.
        Index("idx_edges_ws_ctx_src", "workspace_id", "context_id", "src_id"),
        Index("idx_edges_ws_ctx_dst", "workspace_id", "context_id", "dst_id"),
        Index(
            "idx_edges_origin",
            "origin",
            postgresql_where=text("origin = 'semantic'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<NeuralMemoryEdge(user='{self.user_id}', {self.src_id} -> {self.dst_id}, weight={self.weight:.3f})>"
