"""SQLAlchemy models for memory storage.

Based on Issue #1 - Kagura Memory Cloud backend specification.

Provides ORM models for:
- memories table (3-layer memory with Working/Persistent)
- graph_memory table (NetworkX graph JSON storage)
"""

from uuid import uuid4

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,  # Migration 061: For generated columns
    DateTime,
    Float,
    ForeignKey,  # Migration 062: For context_id FK
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

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
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(String(255), nullable=False, index=True)

    # Migration 063: 3-level isolation (workspace, context, user)
    workspace_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    context_id = Column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Layer 1: 検索用サマリー
    summary = Column(Text, nullable=False)
    summary_embedding_id = Column(UUID(as_uuid=True), nullable=True)

    # Issue #122: Embedding status tracking for transaction integrity
    # Values: 'pending', 'success', 'failed'
    embedding_status = Column(String(20), nullable=False, default="pending")
    embedding_error = Column(Text, nullable=True)

    # Layer 2: 文脈説明
    context_summary = Column(Text, nullable=True)

    # Layer 3: 完全詳細
    content = Column(Text, nullable=False)
    details = Column(JSON, nullable=True)

    # メタデータ（LLM判断）
    type = Column(String(50), nullable=False, index=True)
    importance = Column(Float, nullable=False, default=0.5)
    confidence = Column(Float, nullable=False, default=1.0)
    tags = Column(ARRAY(String), nullable=True)
    context = Column(JSON, nullable=True)

    # Working/Persistent メモリ
    scope = Column(String(20), nullable=False, default="working", index=True)
    long_term = Column(Boolean, nullable=False, default=False)
    promoted_at = Column(DateTime, nullable=True)

    # 利用統計（Consolidation判定用）
    use_count = Column(Integer, nullable=False, default=0)
    last_used_at = Column(DateTime, nullable=True)
    accessed_by_clients = Column(ARRAY(String), nullable=True)
    access_count = Column(Integer, nullable=False, default=0)

    # システム情報
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=True, onupdate=func.now())

    # Logical Delete (Issue #46 Phase 4)
    deleted_at = Column(DateTime, nullable=True)
    deleted_by = Column(String(255), nullable=True)

    # クライアント情報
    client = Column(String(100), nullable=False)
    client_version = Column(String(50), nullable=True)

    # Issue #262: Track memory creation source
    # P2-7: Remove index=True (index created in migration 057)
    source = Column(String(50), nullable=False, default="mcp_remember")

    # Migration 061: Generated columns for efficient resource memory lookups
    # These columns are automatically computed from details JSONB field
    resource_id = Column(
        String(255),
        Computed("details->>'resource_id'", persisted=True),
        index=False,  # Index created in Migration 061
    )
    resource_doc_id = Column(
        String(255),
        Computed("details->>'doc_id'", persisted=True),
        index=False,  # Index created in Migration 061
    )
    resource_version = Column(
        Integer,
        Computed(
            "CASE WHEN details->>'version' ~ '^[0-9]+$' THEN (details->>'version')::INTEGER ELSE NULL END",
            persisted=True,
        ),
        index=False,  # Index created in Migration 061
    )

    # Constraints
    __table_args__ = (
        CheckConstraint("importance BETWEEN 0 AND 1", name="valid_importance"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="valid_confidence"),
        CheckConstraint("scope IN ('working', 'persistent')", name="valid_scope"),
        CheckConstraint(
            "embedding_status IN ('pending', 'success', 'failed')",
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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    memory_id = Column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    data = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

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

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(255), nullable=False, unique=True, index=True)

    # NetworkX グラフ全体（node_link_data形式）
    graph_data = Column(JSON, nullable=False)

    # 統計情報（キャッシュ・監視用）
    total_nodes = Column(Integer, nullable=False, default=0)
    total_edges = Column(Integer, nullable=False, default=0)
    avg_edge_weight = Column(Float, nullable=False, default=0.0)
    max_edge_weight = Column(Float, nullable=False, default=0.0)

    # タイムスタンプ
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    # パフォーマンス監視
    last_decay_at = Column(DateTime, nullable=True)
    last_consolidation_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<GraphMemory(user='{self.user_id}', nodes={self.total_nodes}, edges={self.total_edges})>"


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
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Graph structure
    user_id = Column(String(255), nullable=False, index=True)
    src_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    dst_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    # Migration 062: 3-level isolation (workspace, context, user)
    workspace_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    context_id = Column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Edge properties
    edge_type = Column(String(50), nullable=False, default="neural_association")
    weight = Column(Float, nullable=False, default=0.0)
    confidence = Column(Float, nullable=False, default=1.0)

    # Metadata (flexible JSONB for future extensions)
    # Note: 'metadata' is reserved in SQLAlchemy, use edge_metadata
    edge_metadata = Column("metadata", JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    last_updated = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    # Table constraints
    __table_args__ = (
        CheckConstraint("weight >= 0.0 AND weight <= 3.0", name="valid_weight"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="valid_confidence"),
        CheckConstraint(
            "edge_type IN ('neural_association', 'related_to', 'depends_on', 'learned_from')",
            name="valid_edge_type",
        ),
        Index("idx_edges_user_src", "user_id", "src_id"),
        Index("idx_edges_user_dst", "user_id", "dst_id"),
    )

    def __repr__(self) -> str:
        return f"<NeuralMemoryEdge(user='{self.user_id}', {self.src_id} -> {self.dst_id}, weight={self.weight:.3f})>"
