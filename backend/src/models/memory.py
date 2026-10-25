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
    Double,  # #1331: WHERE-axis generated coordinate columns
    Float,
    ForeignKey,  # Migration 062: For context_id FK
    ForeignKeyConstraint,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from db.base import Base
from models.json_types import NullableJSON

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

# Time Memory lane (#877): rows ``recall_upcoming`` serves deterministically by
# ``type`` + ``details.trigger``. Its write path never assigns
# ``delivery_mode='on_trigger'``, so the lane is keyed on ``type`` alone — every
# reader and every automated-deleter exemption (#1524) must use this constant.
MEMORY_TYPE_TIME = "time"

# ``deleted_by`` provenance for rows Sleep maintenance tombstones (#1209/#1520).
# Merge losers and consolidation archives are both soft deletes that
# ``rollback_sleep_run`` can restore, and both are hard-purged by the SAME
# retention window (``sleep_merge_retention_days``). The set is the single
# source for every lane predicate; writers and guards use these constants.
DELETED_BY_SLEEP_MERGE = "sleep_maintenance"
DELETED_BY_SLEEP_ARCHIVE = "sleep_consolidation"
SLEEP_TOMBSTONE_DELETED_BY: frozenset[str] = frozenset(
    {DELETED_BY_SLEEP_MERGE, DELETED_BY_SLEEP_ARCHIVE}
)

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

# #2043: the fields whose change is a new ``Memory.version``. An edit that
# changes none of them (importance-only is a change; delivery_mode, context,
# a supersede dismissal, access stats are not) neither bumps the version nor
# writes a revision. Order is the order ``MemoryRevision.prior`` keys follow.
VERSIONED_FIELDS: tuple[str, ...] = (
    "summary",
    "context_summary",
    "content",
    "details",
    "type",
    "importance",
    "tags",
)

# #2043/#2044: one ``memory_revisions`` row per write event. The CHECK literals
# are derived from these tuples byte-identically to the alembic migration
# (pinned by tests/test_memory_revision_constants.py). New values are APPENDED,
# never reordered.
REVISION_KIND_CREATED = "created"
REVISION_KIND_UPDATED = "updated"
REVISION_KIND_FORGOTTEN = "forgotten"
REVISION_KIND_RESTORED = "restored"
REVISION_KINDS: tuple[str, ...] = (
    REVISION_KIND_CREATED,
    REVISION_KIND_UPDATED,
    REVISION_KIND_FORGOTTEN,
    REVISION_KIND_RESTORED,
)
# Who wrote the event: an API caller (MCP / REST), a session rollback (#2044)
# or account erasure reverting the details an erased member wrote on another
# author's memory (#2244, ``services.erasure_details``). Sleep maintenance
# writes no revision (its own action log covers it).
REVISION_WRITER_API = "api"
REVISION_WRITER_ROLLBACK = "rollback"
REVISION_WRITER_ERASURE = "erasure"
REVISION_WRITERS: tuple[str, ...] = (
    REVISION_WRITER_API,
    REVISION_WRITER_ROLLBACK,
    REVISION_WRITER_ERASURE,
)

# #2044: ``memory_session_rollbacks.status``. ``in_progress`` = a call stopped
# at the per-call cap and the next call continues; ``partial`` = the last call
# left failed items, which the next call retries; ``completed`` = every event
# is handled, and a further rollback of the session is refused until it
# writes again. Byte-identical to the f04 CHECK literal (pinned by
# tests/test_session_rollback_constants.py); new values are APPENDED.
SESSION_ROLLBACK_IN_PROGRESS = "in_progress"
SESSION_ROLLBACK_COMPLETED = "completed"
SESSION_ROLLBACK_PARTIAL = "partial"
SESSION_ROLLBACK_STATUSES: tuple[str, ...] = (
    SESSION_ROLLBACK_IN_PROGRESS,
    SESSION_ROLLBACK_COMPLETED,
    SESSION_ROLLBACK_PARTIAL,
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

    # Migration 063: 3-level isolation (workspace, context, user).
    # #1951 (e95): NOT NULL — the live memory count (services/memory_counts.py)
    # is workspace-scoped, so a row without a workspace would escape every cap.
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
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
    # #2095: reclaiming a stale `processing` row spends one too, so a run that
    # outlives its claim every time ends `failed` instead of looping.
    # The manual admin retry endpoint resets this to 0.
    embedding_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    # The embedding pipeline's own clock: stamped when a row is claimed
    # (``processing``), when an attempt fails, and when a Sleep undo or
    # rollback restores a ``success`` row and re-embeds it (#2063; an
    # embedding-model switch re-queues rows embedded since its re-embed
    # started). The stale-claim recovery and the #979 retry backoff read it.
    # It used to be ``updated_at``, which made every freshly embedded memory
    # look edited to anyone reading ``updated_at`` as "a person changed this"
    # (``list`` / ``changes_since``, #1852); ``updated_at`` is now written by
    # an edit, a scope promotion or a Sleep maintenance change (importance
    # re-evaluation, dedup tag merge, rollback); never by the initial write or
    # an embedding retry.
    embedding_attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # #2092: the digest of summary + content this row was last embedded with
    # (``services.supersede_verify.embedded_text_digest``). NULL = never
    # successfully embedded since the column exists. Read by
    # ``process_pending_embedding`` to tell a mechanical re-embed (unchanged
    # text: chunk backfill, model switch, repair re-queue) from a first embed
    # or an edit; written only by its success write.
    embedded_text_digest: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Layer 2: 文脈説明
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Layer 3: 完全詳細
    content: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(NullableJSON, nullable=True)

    # メタデータ（LLM判断）
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    context: Mapped[dict[str, Any] | None] = mapped_column(NullableJSON, nullable=True)

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
    # #1985: the bump (with last_used_at / accessed_by_clients, and
    # reference_count for reference()) is best-effort — written with
    # ``FOR UPDATE SKIP LOCKED``, so a surfacing that meets a row another
    # transaction holds is not counted. Treat it as a lower bound.
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
    # #2124 ships content on recall; #2139 counts a reference(*_offset)
    # continuation once per session (its first read of the memory), so a
    # memory read on from a recall cut is counted; one answered from the
    # 1,500-char head alone still is not.
    reference_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )

    # システム情報
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    # #1317: NO ``onupdate`` — ``updated_at`` is the documented staleness cue
    # ("last time the fact was changed"), so it must move only when an edit
    # path sets it explicitly (update/patch/upsert, resource re-index, sleep
    # merge/re-eval all do). With ``onupdate=func.now()`` every read-path
    # access-stats flush (recall/reference/explore) silently re-stamped it.
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # #2043: optimistic-concurrency version. 1 on create; +1 on an edit that
    # changes one of ``VERSIONED_FIELDS`` (each such edit writes an
    # ``updated`` row to ``memory_revisions``). forget, Sleep maintenance, the
    # embedding pipeline and access stats never move it, so an agent's
    # ``update_memory(expected_version=N)`` fails only on a real edit.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    # #2044: the LAST writer's verified agent (``AgentScope``, never a request
    # field) and session (OTel baggage, else the MCP protocol session) — every
    # create, versioned edit and forget (each one a revision row) overwrites
    # both. A display and lookup convenience:
    # the per-write principal lives in ``memory_revisions``, and nothing
    # authorizes on these two columns. NULL on rows written before #2044.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Same width and charset as memory_access_events.session_id.
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

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

    # #1403: server-only supersede suggestion — the top-1 near-duplicate found at
    # ingest by the k-NN seeding (cosine >= _SUPERSEDE_SUGGEST_THRESHOLD), stored
    # as {memory_id, similarity, detected_at}. A DEDICATED column, NOT a key inside
    # the client-writable ``details`` blob: putting it in ``details`` would let a
    # client forge the ``memory_id`` (leaking another memory's summary via the
    # read-path enrichment, bypassing can_access_memory) or silently drop it on a
    # ``details`` replace. Surfaced — liveness-guarded — on recall()/reference(),
    # and cleared when the suggested supersedes edge is created. SQL NULL = none:
    # ``none_as_null`` stores a Python ``None`` as SQL NULL, not the JSON value
    # ``null`` the default writes (#2022), so ``supersede_candidate IS NULL``
    # counts the rows without a suggestion. A non-NULL value is always an object
    # — a candidate, a dismissal tombstone, or both.
    supersede_candidate: Mapped[dict[str, Any] | None] = mapped_column(NullableJSON, nullable=True)

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

    # WHERE axis (#1331): generated numeric coordinates extracted from
    # details.location.lat/lon (validated + 7-decimal-normalized by
    # MemoryService._apply_location on every write path). Unlike the time
    # axis' TEXT trick, ``::double precision`` is IMMUTABLE, so these are real
    # numeric columns. The regex guard NULLs malformed values instead of
    # failing the INSERT (raw-SQL defense). ``details`` is PostgreSQL json
    # (NOT jsonb) — inserted text is stored VERBATIM, and the app write path
    # (json.dumps) renders 0 < |value| < 1e-4 in exponent notation
    # (``5e-05``), so the guard must accept exponent forms too (#1344). The
    # 2-digit exponent cap rejects the exponent-driven extremes: ``1e309``
    # would error at the ``::double precision`` cast (breaking the
    # "malformed -> NULL, never error" contract) and ``1e123`` would cast
    # and then abort the INSERT on the range CHECK. (A 300+-digit
    # plain-decimal lexeme can still error at the cast — raw-SQL-only and
    # unchanged from pre-#1344.) Partial btree (location_lat, location_lon) WHERE
    # location_lat IS NOT NULL AND deleted_at IS NULL is created in migration
    # e69_1331_location_cols.
    location_lat: Mapped[float | None] = mapped_column(
        Double,
        Computed(
            r"CASE WHEN details->'location'->>'lat' ~ "
            r"'^-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]{1,2})?$' "
            "THEN (details->'location'->>'lat')::double precision ELSE NULL END",
            persisted=True,
        ),
        index=False,
    )
    location_lon: Mapped[float | None] = mapped_column(
        Double,
        Computed(
            r"CASE WHEN details->'location'->>'lon' ~ "
            r"'^-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]{1,2})?$' "
            "THEN (details->'location'->>'lon')::double precision ELSE NULL END",
            persisted=True,
        ),
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
        # Issue #1869: partial B-tree for the shared-point check that runs once
        # per forgotten row (MemoryService._delete_memory_point): "does another
        # live row name this point?" filters by summary_embedding_id alone,
        # across contexts. Partial on live rows that have a point, which is
        # exactly the predicate of that query (migration
        # e94_1869_summary_emb_idx).
        Index(
            "idx_memories_summary_embedding_live",
            "summary_embedding_id",
            postgresql_where=text("deleted_at IS NULL AND summary_embedding_id IS NOT NULL"),
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
        # Tool guardrails: partial B-tree backing the deterministic
        # load_guardrails read (MemoryRepository.list_tool_triggered). Scoped
        # to context_id and partial on the presence of details.tool_trigger.
        # ``details`` is PostgreSQL ``json`` (not jsonb) and ``->`` is
        # immutable, so the json-path predicate is index-safe; a stored JSON
        # ``null`` would still count as present, which is why the write path
        # removes the key instead of storing null. The complement predicate
        # (Sleep exclusions) re-parses ``details`` per candidate row; a
        # ``Computed`` boolean (the trigger_from / location_lat precedent) is
        # the upgrade path if Sleep timings ever regress. Created in migration
        # e84_1619_tool_guardrails; the predicate text must stay byte-identical.
        Index(
            "idx_memories_tool_trigger",
            "context_id",
            postgresql_where=text("(details->'tool_trigger') IS NOT NULL AND deleted_at IS NULL"),
        ),
        # #2044: "which memories did this session write last" per context.
        # Partial: rows written before #2044, and writes without a session,
        # carry no entry. Created CONCURRENTLY in migration
        # f04_2044_session_provenance; the predicate text must stay
        # byte-identical.
        Index(
            "idx_memories_context_session",
            "context_id",
            "session_id",
            postgresql_where=text("session_id IS NOT NULL"),
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
        # WHERE axis (#1331) partial btree backing recall_nearby's bbox
        # prefilter. The predicate carries deleted_at IS NULL (an improvement
        # over idx_memories_trigger_from) so the query's tombstone condition
        # must appear verbatim for the index to serve — a missing predicate
        # shows up as a seq scan instead of a silent leak. Created in
        # migration e69_1331_location_cols.
        Index(
            "idx_memories_location",
            "location_lat",
            "location_lon",
            postgresql_where=text("location_lat IS NOT NULL AND deleted_at IS NULL"),
        ),
        # WHERE axis (#1331) range CHECK — raw-SQL defense behind
        # normalize_location. A CHECK passes when the expression is NULL, so
        # rows without a location (both columns NULL from the regex guard)
        # pass; the explicit IS NULL arms make that reading unmissable while
        # a present-but-out-of-range value is a definite FALSE.
        CheckConstraint(
            "(location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)) "
            "AND (location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180))",
            name="valid_location_range",
        ),
        # #1963: a memory lives in its context's workspace. The composite FK
        # targets ``ux_contexts_workspace_id_id`` and sits beside the
        # single-column ``context_id`` FK; both are ON DELETE CASCADE (a
        # NO ACTION one would fire before the other cascades and refuse a
        # context hard delete). ON UPDATE NO ACTION: a context with memories
        # never moves to another workspace. MATCH SIMPLE, so a row without a
        # context stays legal. Two FK paths to ``contexts`` mean every join
        # between the two models needs an explicit ON clause. Created in
        # migration f14_1963_memories_ws_ctx_fk.
        ForeignKeyConstraint(
            ["workspace_id", "context_id"],
            ["contexts.workspace_id", "contexts.id"],
            name="fk_memories_workspace_context",
            ondelete="CASCADE",
        ),
    )

    def __repr__(self) -> str:
        return f"<Memory(id='{self.id}', type='{self.type}', scope='{self.scope}')>"

    @property
    def point_id(self) -> uuid.UUID:
        """The vector store point this row owns (#1829).

        ``summary_embedding_id`` when set — equal to ``id`` for memories the API
        wrote, ``uuid5(resource:doc:version)`` for resource-ingested ones — else
        the row id. Every vector delete must use this, never ``id`` alone.
        """
        return self.summary_embedding_id or self.id

    @property
    def is_pinned(self) -> bool:
        """#1519: the one Python-side definition of "pinned" — the row belongs to the
        deterministic ``load_pinned()`` lane (``delivery_mode='always'``). The SQL
        twin is ``pinned_predicate()`` below; both read ``DELIVERY_MODE_ALWAYS``."""
        return self.delivery_mode == DELIVERY_MODE_ALWAYS

    @property
    def is_tool_triggered(self) -> bool:
        """The one Python-side definition of "tool guardrail": the row carries a
        ``details.tool_trigger`` object and belongs to the deterministic
        ``load_guardrails()`` lane. The SQL twin is ``tool_triggered_predicate()``."""
        return isinstance(self.details, dict) and self.details.get("tool_trigger") is not None


class MemoryRevision(Base):
    """One write event on a memory: the revision log (#2043, shared with #2044).

    An event log, not a snapshot table. ``kind='updated'`` rows carry in
    ``prior`` the values the edit overwrote — the CHANGED keys of
    ``VERSIONED_FIELDS`` only (a JSON ``null`` is "was null", an absent key is
    "unchanged"), so an importance-only edit costs a few bytes, not a copy of
    a 1 MB content. Version K is the live row with the ``prior`` of every
    ``updated`` row above K applied newest first. ``created`` / ``forgotten``
    / ``restored`` rows carry no snapshot (the live row or the tombstone holds
    the text) and only ``created`` / ``updated`` move ``Memory.version``.

    Rows go with their memory: hard deletes (tombstone purge, context delete,
    erasure) cascade through the FK. ``forget`` keeps them — the tombstone
    keeps the same text until it is purged, and every read API answers a
    tombstone with the uniform not-found. Never embedded, never searchable.

    ``actor_user_id`` is the caller who wrote the event (``Memory.user_id`` is
    the creator); ``agent_id`` / ``session_id`` are the caller's verified
    agent and session (#2044). Erasure pseudonymizes the actor and NULLs the
    session. ``context_id`` is copied from the memory row so per-context
    session queries never join ``memories``.
    """

    __tablename__ = "memory_revisions"

    # BIGSERIAL: the keyset cursor and the reverse order of a rollback.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    context_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # ``Memory.version`` right after this event.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    prior: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    writer: Mapped[str] = mapped_column(String(10), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Same width and charset as memory_access_events.session_id.
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Always stamped by the writer with ``utcnow()`` (#2135); the default only
    # covers raw INSERTs.
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    # #2044: the ``memory_session_rollbacks.id`` that handled this event —
    # reversed it or skipped it (the summary names the cause); an item that
    # failed stays NULL and is retried. A handled event is never planned
    # again, which is how a rollback that stopped at the per-call cap
    # continues where it left off. No FK: the rollback row and this one share
    # the context's lifetime.
    undone_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        # Derived like valid_delivery_mode: byte-identical to the f03 literal.
        CheckConstraint(
            f"kind IN ({', '.join(repr(k) for k in REVISION_KINDS)})", name="valid_revision_kind"
        ),
        CheckConstraint(
            f"writer IN ({', '.join(repr(w) for w in REVISION_WRITERS)})",
            name="valid_revision_writer",
        ),
        Index("idx_memory_revisions_memory", "memory_id", "version", "id"),
        Index(
            "idx_memory_revisions_session",
            "context_id",
            "session_id",
            "actor_user_id",
            "id",
            postgresql_where=text("session_id IS NOT NULL"),
        ),
        # #2121: one principal's writes in a time window (rollback_session's
        # window mode, changes_since(by_me=true)). Full, not partial: window
        # events may carry a session id too. A btree indexes NULLs, so a user
        # credential's ``agent_id IS NULL`` is an index condition.
        Index(
            "idx_memory_revisions_actor_at",
            "context_id",
            "actor_user_id",
            "agent_id",
            "at",
        ),
        # #2115: erasure finds the subject's events by actor alone. Full, not
        # partial: the column is NOT NULL. The f10 migration builds it
        # CONCURRENTLY; declared plainly here so ``create_all`` can build it.
        Index("idx_memory_revisions_actor", "actor_user_id"),
    )


# The all-zero uuid stands in for "no agent" in the rollback target key, so a
# user-credential session (agent NULL) has exactly one row per target.
_NIL_UUID_SQL = "'00000000-0000-0000-0000-000000000000'::uuid"


class MemorySessionRollback(Base):
    """One ``rollback_session`` of one session in one context (#2044).

    The durable half of "a second rollback of the same session is refused":
    a ``completed`` row stops the next call while the session has written
    nothing since. ``in_progress`` (stopped at the per-call cap) and
    ``partial`` (failed items left) are continued by the next call, with the
    events no rollback has handled yet (``memory_revisions.undone_by``).

    The target is ``(context_id, session_id, principal_user_id, agent_id)``:
    a session is only ever addressed together with its principal — the caller
    sub and the verified agent of the credential that wrote it.
    ``principal_user_id`` is NULL for a context owner's rollback, which covers
    every principal's events under that session id in the context. ``summary``
    holds the running counts over all calls.

    #2121 window mode: a rollback of the caller's own writes in
    ``[window_since, window_until)``. Its row has ``session_id`` NULL, the
    principal set (never the owner scope) and both window columns set; its
    target is ``(context_id, principal_user_id, agent_id, window_since,
    window_until)``. "Exactly one of session_id / the window" and "the window
    has a principal" are enforced by the service (``SessionScope``), not by a
    CHECK: an erased session row also has a NULL ``session_id``.

    Erasure pseudonymizes ``principal_user_id`` / ``requested_by`` and NULLs
    the subject's ``session_id``; the row goes with its context. The window
    bounds are not personal data and stay.
    """

    __tablename__ = "memory_session_rollbacks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # NULL only after erasure scrubbed the subject's session token.
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    principal_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # #2121: the window of a window-mode rollback (naive UTC), else NULL.
    window_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # Derived from the ordered tuple: byte-identical to the f04 literal.
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in SESSION_ROLLBACK_STATUSES)})",
            name="valid_session_rollback_status",
        ),
        Index(
            "uq_memory_session_rollbacks_target",
            "context_id",
            "session_id",
            text("COALESCE(principal_user_id, '')"),
            text(f"COALESCE(agent_id, {_NIL_UUID_SQL})"),
            unique=True,
        ),
        # #2121: one row per (principal, window). Window rows have a NULL
        # session_id, which the target index above treats as distinct.
        Index(
            "uq_memory_session_rollbacks_window",
            "context_id",
            text("COALESCE(principal_user_id, '')"),
            text(f"COALESCE(agent_id, {_NIL_UUID_SQL})"),
            "window_since",
            "window_until",
            unique=True,
            postgresql_where=text("window_since IS NOT NULL"),
        ),
    )


def pinned_predicate() -> ColumnElement[bool]:
    """The one SQL definition of "pinned" (#1523): ``delivery_mode='always'``.

    Inclusion side — ``list_pinned()`` selects with it. Every automated deleter
    excludes with ``not_pinned_predicate()`` so the invariant "maintenance never
    removes or demotes a pinned memory" is spelled once, not per phase.
    """
    return Memory.delivery_mode == DELIVERY_MODE_ALWAYS


def not_pinned_predicate() -> ColumnElement[bool]:
    """Exclusion side of ``pinned_predicate()`` for candidate queries (#1523).

    ``delivery_mode`` is NOT NULL (server default ``on_recall``), so a plain
    inequality is the exact complement — no NULL branch to keep.
    """
    return Memory.delivery_mode != DELIVERY_MODE_ALWAYS


def tool_triggered_predicate() -> ColumnElement[bool]:
    """The one SQL definition of "tool guardrail": ``details->'tool_trigger'``
    is present. Inclusion side — ``list_tool_triggered()`` selects with it and
    it matches the ``idx_memories_tool_trigger`` partial-index predicate.
    Sibling of ``pinned_predicate()``; the two lanes are independent (a row may
    be both) and neither redefines the other.
    """
    return Memory.details["tool_trigger"].is_not(None)


def not_tool_triggered_predicate() -> ColumnElement[bool]:
    """Exclusion side of ``tool_triggered_predicate()`` for the automated
    deleters (dedup, consolidation, importance re-evaluation).

    ``details`` is nullable: ``NULL -> 'tool_trigger'`` is NULL, so ``IS NULL``
    is the exact complement — a row without details is safely "not a guardrail".
    """
    return Memory.details["tool_trigger"].is_(None)


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
# connector-worker ingest pipeline. They live on the edge_type
# (relation) axis; their provenance is the ``origin`` axis (origin='declared'
# for the worker create_edge path, pinned in mcp_server/tools/edge.py).
#   - continues_from: chronological/narrative successor between chat memories
#   - references_file: structural reference from a chat memory to a file overview
EDGE_TYPE_CONTINUES_FROM = "continues_from"
EDGE_TYPE_REFERENCES_FILE = "references_file"
# Issue #1208: fact-succession relations (the non-destructive update path the
# kagura-memory-eval program found missing — update-by-removal was the only
# mechanism). Direction convention, load-bearing for recall shadowing:
#   src = the SUPERSEDING (newer/current) memory
#   dst = the SUPERSEDED (older/stale) memory
# A memory that is the dst of a supersedes edge is "shadowed": recall demotes
# it out of results (opt back in with include_superseded=true) as long as the
# superseding src is still alive — a deleted superseder stops shadowing
# (self-healing JOIN in the shadow query; edge refs are bare UUIDs, no FK).
# 'contradicts' NEVER hides either side — recall annotates both (resolution
# is an arbitration problem, not a deletion problem). Deliberately NOT in
# edge_discovery's LLM_EMITTABLE_EDGE_TYPES: the sleep judge must not invent
# supersession (structural over-supersede prevention).
EDGE_TYPE_SUPERSEDES = "supersedes"
EDGE_TYPE_CONTRADICTS = "contradicts"

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
    # #1208: appended (never reordered) per the byte-identity rule above.
    EDGE_TYPE_SUPERSEDES,
    EDGE_TYPE_CONTRADICTS,
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
    edge_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", NullableJSON, nullable=True
    )

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
