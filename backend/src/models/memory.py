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
    DDL,
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
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Delivery mode constants (Issue #886, design memory 242cb28a) — an ORTHOGONAL
# delivery attribute on Memory, deliberately NOT a new memory ``type``. It
# controls *when* a memory is surfaced, independent of *what* it is about:
#   - always:     deterministically loaded every turn (Goal / Guardrail / policy)
#   - on_recall:  surfaced only via probabilistic recall() (the default)
#   - on_trigger: surfaced when a trigger fires — already realized as Time Memory
#                 (type='time' + details.trigger + Computed trigger_from)
# The DB CHECK constraint is generated from the ordered ``_ALL_DELIVERY_MODES``
# tuple (see ``Memory.__table_args__``), mirroring the edge_type/origin pattern on
# NeuralMemoryEdge. New modes are APPENDED (never reordered) to preserve
# byte-identity with the alembic migration literal.
DELIVERY_MODE_ALWAYS = "always"
DELIVERY_MODE_ON_RECALL = "on_recall"
DELIVERY_MODE_ON_TRIGGER = "on_trigger"

_ALL_DELIVERY_MODES: tuple[str, ...] = (
    DELIVERY_MODE_ALWAYS,
    DELIVERY_MODE_ON_RECALL,
    DELIVERY_MODE_ON_TRIGGER,
)

# Issue #887: server-authoritative provenance. ``source_type`` records HOW a
# memory entered the system; it is NOT NULL + CHECK (tuple-derived, test-pinned,
# same discipline as delivery_mode / edge origin). Values
# file/url/vault/api/manual are user-origin provenance (legitimately client-set
# on import — #213/#262);
# ``connector`` is server-only (stamped by resource_indexer for external
# ingestion). Trust is NOT carried here — it is authoritative at the context
# level (``Context.trust_tier``); source_type is provenance, not a trust claim.
# New values are APPENDED (never reordered) to preserve byte-identity with the
# alembic migration literal.
SOURCE_TYPE_FILE = "file"
SOURCE_TYPE_URL = "url"
SOURCE_TYPE_VAULT = "vault"
SOURCE_TYPE_API = "api"
SOURCE_TYPE_MANUAL = "manual"
SOURCE_TYPE_CONNECTOR = "connector"

_ALL_SOURCE_TYPES: tuple[str, ...] = (
    SOURCE_TYPE_FILE,
    SOURCE_TYPE_URL,
    SOURCE_TYPE_VAULT,
    SOURCE_TYPE_API,
    SOURCE_TYPE_MANUAL,
    SOURCE_TYPE_CONNECTOR,
)


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
        summary: Layer 1 - 検索用サマリー (50-200文字). Backed by a pg_trgm
            GIN index (``idx_memories_summary_trgm``, #818) accelerating the
            ``summary ILIKE '%q%'`` substring filter on GET /memory/list.
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
        last_used_at: 最終使用時刻
        accessed_by_clients: アクセスしたクライアント一覧
        access_count: アクセス回数 (surfacing: recall top-k 返却 + explore 拡散 + reference)
        reference_count: 採用シグナル (#1046: reference() のみが加算; surfacing と区別)
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
    # Values: 'pending', 'processing', 'success', 'failed'
    embedding_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    embedding_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Issue #979: bounded auto-requeue of transiently-failed embeddings. The
    # sweep re-claims `failed` rows while this is below MAX_EMBEDDING_RETRIES,
    # incrementing it per retry; a poison row exhausts its budget and stops.
    # The manual admin retry endpoint resets this to 0.
    embedding_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )

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

    # Issue #886: orthogonal delivery attribute (sibling of ``scope``, NOT a
    # new ``type``). Default 'on_recall' = current probabilistic-only behavior,
    # so existing rows are unaffected. server_default keeps the migration
    # backfill and ORM default in lock-step. The partial index supporting the
    # deterministic always-load read path is created in the alembic migration
    # (on (context_id, delivery_mode) WHERE delivery_mode='always').
    delivery_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=DELIVERY_MODE_ON_RECALL,
        server_default=DELIVERY_MODE_ON_RECALL,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 利用統計（Consolidation判定用）
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accessed_by_clients: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    # ``access_count`` counts *surfacing*: every recall top-k return + explore
    # spreading-activation hit + reference(). It is the signal Sleep
    # consolidation gates on today (unchanged by #1046).
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Issue #1046: adoption signal — bumped ONLY by reference() (a deliberate
    # Layer-3 fetch), distinct from the surfacing-inflated ``access_count``. The
    # invariant ``access_count >= reference_count`` always holds because every
    # reference() also bumps access_count.
    #
    # PROXY BIAS (read before consuming — #1048 ranking, #1049 consolidation):
    # reference() returns Layer-3 (content+details), but recall() already returns
    # Layer 1-2 (summary+context_summary), which is frequently *enough*. So
    # reference_count systematically UNDER-COUNTS adoptions satisfied from the
    # summary alone, and is biased toward long / detail-heavy memories that force
    # a Layer-3 fetch. Downstream consumers MUST account for this bias.
    reference_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )

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
    # Issue #887: NOT NULL + CHECK; legacy NULL rows backfilled to 'manual' in
    # the alembic migration. server_default keeps the migration backfill and ORM
    # default in lock-step. Values are constrained by valid_source_type below.
    source_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SOURCE_TYPE_MANUAL,
        server_default=SOURCE_TYPE_MANUAL,
    )

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

    # Time Memory (type="time"): generated lower/upper bounds of the trigger
    # window, extracted as TEXT from details.trigger.from/until (naive ISO
    # strings derived in MemoryService.remember). Mirrors the external_blob_*
    # Computed pattern — a plain ``->>'from'`` extraction is IMMUTABLE, whereas
    # a ``::timestamp`` cast is only STABLE and PostgreSQL rejects it in a STORED
    # generated column. The from/until strings are fixed-width zero-padded ISO
    # (YYYY-MM-DDTHH:MM:SS), so lexical comparison == chronological comparison,
    # which is what the window-overlap filter + ORDER BY trigger_from rely on.
    # Partial btree index on trigger_from WHERE type='time' is created in
    # migration e30_877_time_trigger_cols.
    trigger_from: Mapped[str | None] = mapped_column(
        String(32),
        Computed("details->'trigger'->>'from'", persisted=True),
        index=False,
    )
    trigger_until: Mapped[str | None] = mapped_column(
        String(32),
        Computed("details->'trigger'->>'until'", persisted=True),
        index=False,
    )

    # Constraints
    __table_args__ = (
        CheckConstraint("importance BETWEEN 0 AND 1", name="valid_importance"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="valid_confidence"),
        CheckConstraint("scope IN ('working', 'persistent')", name="valid_scope"),
        # Issue #886: CHECK derived from ``_ALL_DELIVERY_MODES`` (registration
        # order, single quotes, exact whitespace), byte-identical to the alembic
        # migration literal. Drift is pinned by
        # test_valid_delivery_mode_check_constraint_matches_migration_literal.
        CheckConstraint(
            f"delivery_mode IN ({', '.join(repr(d) for d in _ALL_DELIVERY_MODES)})",
            name="valid_delivery_mode",
        ),
        # Issue #887: CHECK derived from ``_ALL_SOURCE_TYPES`` (registration
        # order, single quotes), byte-identical to the alembic migration literal.
        # Drift is pinned by
        # test_valid_source_type_check_constraint_matches_migration_literal.
        CheckConstraint(
            f"source_type IN ({', '.join(repr(s) for s in _ALL_SOURCE_TYPES)})",
            name="valid_source_type",
        ),
        CheckConstraint(
            "embedding_status IN ('pending', 'processing', 'success', 'failed')",
            name="valid_embedding_status",
        ),
        # Indexes
        Index("idx_user_scope", "user_id", "scope"),
        Index("idx_user_type", "user_id", "type"),
        # Issue #1046: dropped the stale ``idx_consolidation`` index. It indexed
        # the dead ``use_count`` column and its leading (user_id, long_term)
        # columns never matched the consolidation candidate query, which filters
        # ``scope == 'working'`` (covered by ``idx_user_scope``).
        Index("idx_created_at", "created_at"),
        Index("idx_last_used", "last_used_at"),
        # JSONB index for context_id
        Index("idx_context", func.cast(context["context_id"], String)),
        # Issue #213 partial B-tree (migration a95_source_uri_declared_link).
        Index(
            "idx_memories_source_uri",
            "source_uri",
            postgresql_where=text("source_uri IS NOT NULL"),
        ),
        # Issue #223 GIN index on tags array (migration b05_223_tag_cooccurrence).
        Index("idx_memories_tags_gin", "tags", postgresql_using="gin"),
        # Issue #485 partial B-tree on the generated external_blob_ref column
        # (migration e03_485_file_objects).
        Index(
            "idx_memories_external_blob_ref",
            "external_blob_ref",
            postgresql_where=text("external_blob_ref IS NOT NULL"),
        ),
        # Issue #818 trigram GIN index on summary, accelerating the
        # `summary ILIKE '%q%'` substring filter on GET /memory/list (#580)
        # (migration e26_818_summary_trgm_idx). Indexed on the bare
        # column — gin_trgm_ops serves ILIKE case-insensitively, so no
        # lower(summary) expression is needed and the #580 query is unchanged.
        Index(
            "idx_memories_summary_trgm",
            "summary",
            postgresql_using="gin",
            postgresql_ops={"summary": "gin_trgm_ops"},
        ),
        # Issue #979 partial index for the embedding sweep (migration e40).
        # The sweep runs every 30s over not-yet-settled rows (pending /
        # processing / failed); the partial predicate keeps it scanning only
        # that normally-tiny set instead of the whole (mostly 'success')
        # memories table as it grows.
        Index(
            "idx_memories_embedding_unsettled",
            "embedding_status",
            postgresql_where=text("embedding_status <> 'success'"),
        ),
        # Issue #619 compound partial B-tree on (workspace_id, context_id),
        # accelerating the scope scan used by aggregate_tags, get_context_stats,
        # and _refresh_hub_tag_cache (migration e29_619_memories_ws_ctx_idx).
        # Partial on deleted_at IS NULL keeps soft-deleted rows out of the index.
        Index(
            "idx_memories_ws_ctx",
            "workspace_id",
            "context_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Issue #886: partial B-tree supporting the deterministic always-load
        # read path (load_pinned). Scopes to context_id and is partial on
        # delivery_mode='always' AND deleted_at IS NULL so only pinned, live
        # rows carry the index — the always-set is bounded and small per
        # context. Created in migration e32_886_delivery_mode.
        Index(
            "idx_memories_delivery_always",
            "context_id",
            postgresql_where=text("delivery_mode = 'always' AND deleted_at IS NULL"),
        ),
        # Time Memory (type="time") partial btree on the generated lower bound,
        # supporting the window-overlap query + ORDER BY trigger_from on
        # GET /memory/list (migration e30_877_time_trigger_cols). Partial so
        # only time memories carry the index.
        Index(
            "idx_memories_trigger_from",
            "trigger_from",
            postgresql_where=text("type = 'time'"),
        ),
        # Defense-in-depth for the lexical==chronological invariant: a
        # type="time" row must carry both window bounds in fixed-width
        # zero-padded ISO (NULL fails the regex, so this also enforces
        # presence). Gated on type<>'time' so other memory types are
        # unaffected even if they use a details.trigger.* path.
        CheckConstraint(
            "type <> 'time' OR ("
            "trigger_from IS NOT NULL "
            "AND trigger_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$' "
            "AND trigger_until IS NOT NULL "
            "AND trigger_until ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$')",
            name="valid_trigger_window_format",
        ),
    )

    def __repr__(self) -> str:
        return f"<Memory(id='{self.id}', type='{self.type}', scope='{self.scope}')>"


# The ``idx_memories_summary_trgm`` GIN index (#818) uses the ``gin_trgm_ops``
# operator class, which only exists once the ``pg_trgm`` extension is installed.
# Alembic migration e26 installs it on the upgrade path, but the
# ``Base.metadata.create_all()`` path (test session setup + the create_all-vs-
# alembic drift guard, which runs after ``DROP SCHEMA public CASCADE`` wipes the
# extension) has no such step — create_all would fail emitting the index. This
# ``before_create`` hook installs the extension first so the model is fully
# create_all-able. Guarded to PostgreSQL so non-PG dialects (e.g. SQLite in unit
# tests) skip it; ``IF NOT EXISTS`` keeps it idempotent against the migrated DB.
event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS pg_trgm").execute_if(dialect="postgresql"),
)


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
        return f"<GraphMemory(id={self.id}, user='{self.user_id}')>"


# Edge type constants — named aliases for the persisted edge_type string values.
# Python validators (#461 PR #507, #506 PR #508) reference these constants by name.
# The DB CHECK constraint is generated from the ordered ``_ALL_EDGE_TYPES`` tuple
# (see below), which enumerates the same values in registration order.
EDGE_TYPE_NEURAL_ASSOCIATION = "neural_association"
EDGE_TYPE_RELATED_TO = "related_to"
EDGE_TYPE_DEPENDS_ON = "depends_on"
EDGE_TYPE_LEARNED_FROM = "learned_from"
# Issue #782: producer-asserted structural relation types emitted by the
# kagura-memory-ai-worker ingest pipeline. They live on the edge_type
# (relation) axis; their provenance is the ``origin`` axis (origin='declared'
# for the worker create_edge path, pinned in mcp_server/tools/edge.py).
#   - continues_from: chronological/narrative successor between chat memories
#   - references_file: structural reference from a chat memory to a file overview
EDGE_TYPE_CONTINUES_FROM = "continues_from"
EDGE_TYPE_REFERENCES_FILE = "references_file"

# Issue #509 (Phase B of #461): registration-order tuple used to derive the
# ``valid_edge_type`` CHECK constraint string in ``NeuralMemoryEdge.__table_args__``.
# Order MUST match the literal order the latest CHECK-altering migration installed
# on prod (currently ``e25_782_widen_edge_type.py``'s ``_NEW_CHECK_SQL``; previously
# ``e20_741`` then ``b05_223``) so ``Base.metadata.create_all()`` produces a CHECK
# string byte-identical to alembic head — preventing ``alembic revision
# --autogenerate`` from generating a spurious no-op migration on future runs. New
# edge_types are APPENDED (never reordered) to preserve byte-identity with prior
# literals.
_ALL_EDGE_TYPES: tuple[str, ...] = (
    EDGE_TYPE_NEURAL_ASSOCIATION,
    EDGE_TYPE_RELATED_TO,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_LEARNED_FROM,
    EDGE_TYPE_CONTINUES_FROM,
    EDGE_TYPE_REFERENCES_FILE,
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
        # The f-string output is byte-identical to the latest CHECK-altering
        # migration's literal (currently ``e25_782_widen_edge_type.py``'s
        # ``_NEW_CHECK_SQL``; previously ``e20_741`` then ``b05_223``) — registration
        # order, single quotes, exact whitespace.
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
