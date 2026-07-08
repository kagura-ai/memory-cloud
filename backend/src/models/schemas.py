"""Pydantic schemas for API request/response validation.

Based on Issue #1 - API specifications.
"""

import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from auth.workspace_roles import ContextRole, WorkspaceRole
from models.api_base import TZAwareBaseModel

logger = logging.getLogger(__name__)

# ============================================================================
# OAuth2 Token Introspection (RFC 7662, Issue #157)
# ============================================================================


class TokenIntrospectionResponse(BaseModel):
    """OAuth 2.0 Token Introspection Response (RFC 7662).

    Issue #157: Type-safe response model for /introspect endpoint.

    Attributes:
        active: Whether token is active
        client_id: Client identifier (if active)
        scope: Granted scopes (if active)
        exp: Expiration timestamp (if active)
        iat: Issued-at timestamp (if active)
        token_type: Token type (if active)
        aud: Audience/Resource (RFC 8707, if present)
    """

    active: bool
    client_id: str | None = None
    scope: str | None = None
    exp: int | None = None
    iat: int | None = None
    token_type: str | None = None
    aud: str | None = None  # RFC 8707 audience claim


# ============================================================================
# Memory API Schemas
# ============================================================================


class RememberRequest(BaseModel):
    """Request schema for remember() API.

    Example:
        {
            "summary": "認証エラー修正。JWTトークン有効期限チェック追加。",
            "context_summary": "ユーザーからログイン失敗の報告があり...",
            "content": "auth.pyのverify_token関数にexpired_atの検証を追加",
            "details": {"code_diff": "...", "test_results": "..."},
            "type": "code",
            "importance": 0.8,
            "tags": ["python", "authentication"],
            "context": {"context_id": "my-context", "file_path": "auth.py"}
        }
    """

    # Layer 1: 検索用サマリー (必須)
    summary: str = Field(..., min_length=10, max_length=500, description="検索用サマリー")

    # Layer 2: 文脈説明 (推奨)
    context_summary: str | None = Field(default=None, max_length=2000, description="背景・文脈説明")

    # Layer 3: 完全詳細
    content: str = Field(..., min_length=1, description="基本内容")
    details: dict | None = Field(default=None, description="完全詳細（JSONB）")

    # メタデータ
    type: str = Field(..., min_length=1, max_length=50, description="メモリタイプ")
    importance: float = Field(default=0.5, ge=0.0, le=1.0, description="重要度 (0.0-1.0)")
    tags: list[str] = Field(default_factory=list, description="タグ")
    context: dict | None = Field(default=None, description="コンテキスト情報")

    # Issue #886: orthogonal delivery attribute (NOT a memory type). 'always'
    # pins the memory to persistent on write (deterministic always-load); the
    # default 'on_recall' is the legacy probabilistic-only behavior. The Literal
    # set is pinned to models.memory._ALL_DELIVERY_MODES by a cross-module drift
    # test. 'on_trigger' is realized via Time Memory (type='time').
    delivery_mode: Literal["always", "on_recall", "on_trigger"] = Field(
        default="on_recall",
        description="Delivery mode: always (pin, load every turn) | on_recall (default) | on_trigger",
    )

    # Issue #213: Origin tracking for external integration
    source_uri: str | None = Field(
        default=None,
        max_length=2048,
        description="Origin URI: file://, http(s)://, vault://, obsidian://",
    )
    source_type: Literal["file", "url", "vault", "api", "manual"] | None = Field(
        default=None, description="Origin type"
    )

    # Issue #215: Explicit links (declared_link edges)
    linked_memory_ids: list[UUID] | None = Field(
        default=None,
        description="Explicit links to existing memories (creates declared_link edges)",
    )
    linked_source_uris: list[str] | None = Field(
        default=None,
        description="Explicit links by source_uri (resolved at remember time, unresolved silently skipped)",
    )
    # #1208: fact succession — this new memory supersedes an existing one.
    # Creates a supersedes edge (src=new, dst=old, origin='declared'); the old
    # memory is shadowed out of default recall but never deleted (reachable
    # via include_superseded / explore; deleting the edge restores it fully).
    supersedes: UUID | None = Field(
        default=None,
        description="Memory ID this new memory supersedes (old one is shadowed, not deleted)",
    )

    @field_validator("summary")
    @classmethod
    def validate_summary_length(cls, v: str) -> str:
        """Validate summary length and warn if approaching maximum.

        Optimal summary length: 100-250 characters
        Warning threshold: 400 characters (80% of max 500)
        Maximum: 500 characters (enforced by Field constraint)
        """
        length = len(v)

        if length > 400:
            logger.warning(
                f"Summary length ({length} chars) exceeds recommended 250 chars. "
                f"Consider splitting into multiple semantic memories for better search quality. "
                f"See docs: /docs/chunking-guide.md"
            )
        elif length < 50:
            logger.info(
                f"Summary length ({length} chars) is quite short. "
                f"Consider adding more context for better semantic matching (optimal: 100-250 chars)."
            )

        return v


class RememberResponse(BaseModel):
    """Response schema for remember() API."""

    status: str = "success"
    memory_id: UUID
    scope: str


class RecallRequest(BaseModel):
    """Request schema for recall() API (Hybrid Search).

    Example:
        {
            "query": "認証エラーの解決方法",
            "k": 5,
            "use_rerank": false,
            "filters": {
                "context_id": "my-context",
                "scope": "persistent",
                "type": "code"
            }
        }
    """

    query: str = Field(..., min_length=1, description="検索クエリ")
    k: int = Field(default=5, ge=1, le=100, description="返却結果数")
    use_rerank: bool = Field(default=False, description="Reranking (Voyage/Cohere)を使用")
    filters: dict | None = Field(default=None, description="オプショナルフィルタ")
    search_mode: str = Field(
        default="hybrid",
        pattern="^(hybrid|semantic|keyword)$",
        description="Search mode: hybrid (default), semantic (vector only), keyword (BM25 only)",
    )
    include_explore_hints: bool = Field(
        default=False,
        description="Include up to 3 explore_hints in response suggesting good seeds for explore()",
    )
    # #1208: shadowed (superseded) memories are demoted out of results by
    # default; true returns them annotated with superseded_by for audit.
    include_superseded: bool = Field(
        default=False,
        description="Include memories shadowed by a supersedes edge (annotated with superseded_by)",
    )


class MemoryResponse(TZAwareBaseModel):
    """Response schema for single memory."""

    memory_id: UUID
    summary: str
    context_summary: str | None
    type: str
    importance: float
    scope: str
    created_at: datetime
    # Issue #1047: per-memory recency/staleness cue for the agent consumer.
    # ``updated_at`` is the last real change (None if never edited since create),
    # so an agent can self-assess whether a fact may be stale without extra calls.
    updated_at: datetime | None = None
    client: str
    tags: list[str]
    context: dict | None
    score: float | None = Field(None, description="検索スコア（recall時のみ）")
    source_uri: str | None = None
    # Issue #887: response models must allow 'connector' (the server-stamped
    # value on connector-ingested memories); recall/reference of a connector
    # memory would otherwise raise a Pydantic ValidationError. The *request*
    # schema (RememberRequest) intentionally omits it — the forge guard.
    source_type: Literal["file", "url", "vault", "api", "manual", "connector"] | None = None
    # #1208: fact-succession annotations. superseded_by is set only when the
    # memory is shadowed AND include_superseded=true opted it back into the
    # results (default recall filters shadowed memories out entirely).
    # contradicts lists memories linked by a contradicts edge (either
    # direction) — contradiction never hides, it annotates both sides.
    superseded_by: UUID | None = None
    contradicts: list[UUID] = Field(default_factory=list)

    class Config:
        from_attributes = True


class PinnedMemoryItem(TZAwareBaseModel):
    """A single always-load memory (Issue #886).

    Deliberately L1 + L2 only (summary + context_summary) — the deterministic
    always-load path injects these into the agent's context every turn, so the
    full L3 ``content`` is intentionally omitted and fetched on demand via
    ``reference(memory_id)``. No ``score`` field: this path is unranked.
    """

    memory_id: UUID
    summary: str
    context_summary: str | None
    type: str
    importance: float
    delivery_mode: str
    created_at: datetime

    class Config:
        from_attributes = True


class LoadPinnedRequest(BaseModel):
    """Request for the deterministic always-load read path (Issue #886).

    ``context_id`` selects which context's always-load set to return (the set
    is per-context). ``cap`` optionally overrides ``settings.pinned_load_cap``.
    """

    context_id: str | None = Field(
        default=None, description="Context UUID whose pinned memories to load"
    )
    cap: int | None = Field(
        default=None, ge=1, le=1000, description="Optional override for the max returned (bounded)"
    )


class LoadPinnedResponse(BaseModel):
    """Response for the deterministic always-load read path (Issue #886).

    ``memories`` is the complete, unranked, ordered set up to ``cap``. When the
    context holds more pinned memories than ``cap``, ``truncated`` is true and
    ``total_available`` reports the real count — the set is never silently cut.
    """

    status: str = "success"
    memories: list[PinnedMemoryItem]
    total_available: int
    truncated: bool
    cap: int


class RelatedTagItem(TZAwareBaseModel):
    """Related tag with count, sample summary, and last-used timestamp.

    Used by ``recall.related_tags`` (Issue #104 — populates ``sample_summary``)
    and ``list_tags`` (Issue #614 — populates ``last_used_at``). The shared
    schema means clients can unify their "tag info" type across both surfaces.
    Inherits ``TZAwareBaseModel`` so the optional ``last_used_at`` serializes
    with an explicit UTC ``Z`` suffix when populated.
    """

    tag: str
    count: int
    sample_summary: str | None = None
    last_used_at: datetime | None = None


class ExploreHint(BaseModel):
    """Hint suggesting a memory as a good seed for explore().

    Issue #216: Opt-in field to bridge recall → explore discovery.
    """

    memory_id: UUID
    reason: Literal["top_result", "high_centrality", "unexplored_neighbor"]


class RecallConfidence(BaseModel):
    """Issue #1047/#1052: a signal distinguishing "relevant results found" from
    "likely nothing relevant" (so an agent can stop probing / go external instead
    of trusting a weak hit), WITHOUT changing ranking.

    ``level`` is driven by **absolute semantic match strength**, made robust to
    embedding-model scale via ``prominence`` (#1052):

    - ``top_score`` — the RAW semantic cosine of the best hit (absolute match
      strength in [0, 1]). High ⇒ something closely matches the query.
    - ``prominence`` — ``(top_score - mean_background_cosine) /
      mean_background_cosine`` over the candidate pool. A *ratio*, so it is
      invariant to the overall cosine scale a given embedding model produces
      (text-embedding-3-small and qwen3-embedding sit at different absolute
      cosines, but a real match is proportionally far above its background in
      both). ``level`` thresholds on this; a near-duplicate ``top_score`` floors
      ``level`` to at least ``moderate``.

    How an agent should use it (it is a TRIAGE hint, not a correctness verdict):

    - ``none`` / ``low`` → likely nothing relevant; prefer an external source over
      forcing an answer out of these results. This is the high-value signal and is
      reliable without a reranker.
    - ``high`` / ``moderate`` → relevant memory is likely present; READ the returned
      summaries and judge from their content. ``level`` measures *topical match
      strength*, so a closely-related "near-miss" (an adjacent topic) can also read
      ``high`` — it does NOT guarantee the exact fact you asked for is stored. The
      returned content is the source of truth; ``level`` only says whether it is
      worth reading. Distinguishing near-miss from an exact match needs a
      cross-encoder (pass ``use_rerank=true``); plain bi-encoder cosine cannot.

    Why this supersedes the original #1047 z-score approach: that bucketed on
    ``relative_margin`` (background std-devs), but max-normalization in the hybrid
    merge erases absolute strength, and dividing by a flat off-topic tail's tiny
    std *inflated* the margin — so irrelevant queries scored ``high`` (benchmarked).
    ``relative_margin`` is retained for transparency but is NO LONGER the basis for
    ``level``; note it reads high even for off-topic queries.

    ``prominence`` / ``relative_margin`` are None for keyword-only searches (no
    semantic cosines) and single-candidate pools; there ``level`` falls back to the
    original z-score separation heuristic.
    """

    level: Literal["high", "moderate", "low", "none"]
    top_score: float | None = Field(
        None,
        description=(
            "Raw semantic cosine of the best hit (absolute match strength, 0-1). "
            "On keyword-only searches this is the normalized hybrid top instead."
        ),
    )
    prominence: float | None = Field(
        None,
        description=(
            "(top_score - mean_background_cosine) / mean_background_cosine — a "
            "model-scale-invariant match prominence and the primary basis for "
            "level. None when <2 candidates or keyword-only."
        ),
    )
    relative_margin: float | None = Field(
        None,
        description=(
            "Separation of the top hit from the candidate-pool background, in "
            "background std-devs. Informational only — it inflates on flat "
            "off-topic tails and is not used for level. None when <2 candidates."
        ),
    )
    result_count: int
    rationale: str


class RecallResponse(BaseModel):
    """Response schema for recall() API.

    Issue #104: Added related_tags to help LLMs understand tag context.
    Issue #216: Added explore_hints for graph discovery bridging.
    Issue #1047: Added confidence (relevance/staleness signal) — additive.
    """

    results: list[MemoryResponse]
    related_tags: list[RelatedTagItem] = []
    explore_hints: list[ExploreHint] | None = None
    # Issue #1047: top-level relevance confidence. None on legacy/explore paths
    # that don't compute it; recall() always populates it (incl. "none" on empty).
    confidence: RecallConfidence | None = None


class ReferenceRequest(BaseModel):
    """Request schema for reference() API."""

    memory_id: UUID


class LinkedMemoryRef(TZAwareBaseModel):
    """A single declared_link reference surfaced in ReferenceResponse.

    Issue #440: outgoing_links / incoming_links items. Edge invariant
    (`_validate_edge_context_invariant`) guarantees both endpoints share
    the same (workspace_id, context_id) as the source memory, so a
    workspace-scoped permission check on the source covers these too.
    The bulk re-scope in MemoryService.reference() is defense-in-depth
    against soft-deleted or invariant-violating rows.
    """

    memory_id: UUID
    summary: str
    type: str | None = None
    importance: float
    weight: float
    created_at: datetime


class ReferenceResponse(TZAwareBaseModel):
    """Response schema for reference() API (full details)."""

    memory_id: UUID
    summary: str
    context_summary: str | None
    content: str
    details: dict | None
    type: str
    # Issue #434: scope and updated_at let the dialog render correctly when
    # the caller has only a memory_id (deep-link path), without round-tripping
    # through `GET /memory/list` to discover them.
    scope: Literal["working", "persistent"]
    importance: float
    tags: list[str]
    context: dict | None
    created_at: datetime
    updated_at: datetime
    client: str
    # Optional origin metadata (Issue #215). Surfaced so the detail UI can
    # show where a memory came from (vault://, file://, https://, etc.).
    # Use the same Literal as MemoryResponse / RememberRequest so the OpenAPI
    # contract stays consistent and unexpected values can't leak to the UI.
    source_uri: str | None = None
    # Issue #887: response models must allow 'connector' (the server-stamped
    # value on connector-ingested memories); recall/reference of a connector
    # memory would otherwise raise a Pydantic ValidationError. The *request*
    # schema (RememberRequest) intentionally omits it — the forge guard.
    source_type: Literal["file", "url", "vault", "api", "manual", "connector"] | None = None
    # Issue #440: declared_link references for the dialog "References" section.
    # Naming: outgoing_has_more / incoming_has_more matches MemoryListResponse.has_more
    # (codebase precedent for capped collections); the issue body's `*_truncated`
    # suggestion is intentionally overridden for consistency.
    outgoing_links: list[LinkedMemoryRef] = Field(default_factory=list)
    outgoing_has_more: bool = False
    incoming_links: list[LinkedMemoryRef] = Field(default_factory=list)
    incoming_has_more: bool = False


class ExportedSearchConfig(BaseModel):
    """Per-context search configuration in a portability export (Issue #950).

    #1207: reinforce settings are exported so an explicit opt-out survives the
    portability boundary — without them, re-creating an exported context would
    silently pick up the new default-on. Defaults mirror the current ORM
    defaults so pre-#1207 export documents (which lack these keys) still parse.
    """

    semantic_weight: float
    bm25_weight: float
    fetch_factor: int
    use_rerank: bool
    reranker_provider: str | None
    reranker_model: str | None
    embedding_model: str
    embedding_dimensions: int
    reinforce_enabled: bool = True
    reinforce_max_boost: float = 0.15
    reinforce_require_host_arbitration: bool = False


class ExportedMemory(TZAwareBaseModel):
    """A single memory in a portability export (Issue #950).

    Excludes regenerable fields (vector / embedding status), DB-computed
    columns, internal owner/workspace identifiers, and soft-delete state —
    only the user-meaningful, portable fields are emitted. ``details`` is
    included verbatim; note it may carry references (e.g. external blob
    pointers) that will not resolve outside this deployment.
    """

    id: UUID
    summary: str
    context_summary: str | None
    content: str
    details: dict | None
    type: str
    importance: float
    confidence: float
    tags: list[str]
    context: dict | None
    scope: str
    delivery_mode: str
    created_at: datetime
    updated_at: datetime | None
    source_uri: str | None = None
    source_type: str | None = None


class ExportedContextMeta(TZAwareBaseModel):
    """Context metadata block in a portability export (Issue #950)."""

    id: UUID
    name: str
    display_name: str | None
    description: str | None
    summary: str | None
    usage_guide: str | None
    is_private: bool
    is_public: bool
    created_at: datetime
    updated_at: datetime | None


class ContextExportResponse(TZAwareBaseModel):
    """GDPR Art.20-supporting context portability export (Issue #950).

    A point-in-time JSON snapshot of a context the caller can read: its
    metadata, search configuration, and every memory visible to the caller
    (private context -> creator-only; shared -> all members) — the same
    visibility model as ``GET /memory/list``. Vectors, neural edges, and
    sessions are intentionally omitted: they are regenerated / re-learned on
    re-import. This is a context-scoped *portability* export (anti-lock-in),
    not a per-user Art.20 subject-access request — a created_by-filtered,
    cross-context personal-data export is a separate follow-up.
    """

    format_version: str = "1.0"
    exported_at: datetime
    context: ExportedContextMeta
    search_config: ExportedSearchConfig | None
    memory_count: int
    memories: list[ExportedMemory]


class ForgetRequest(BaseModel):
    """Request schema for forget() API."""

    memory_id: UUID | None = Field(default=None, description="削除するメモリID")
    query: str | None = Field(default=None, description="削除する検索クエリ")
    k: int = Field(default=10, ge=1, le=100, description="削除する結果数（query指定時）")


class ForgetResponse(BaseModel):
    """Response schema for forget() API."""

    status: str = "success"
    deleted_count: int
    memory_ids: list[UUID]


class UpdateMemoryRequest(BaseModel):
    """Request schema for update_memory() API.

    Two modes:
    - In-place update: provide memory_id (preserves ID, graph edges, created_at)
    - Upsert by external_id: provide external_id (forget + remember internally)
    """

    # Identifier (exactly one required)
    memory_id: UUID | None = Field(None, description="Memory UUID for in-place update")
    external_id: str | None = Field(
        None, max_length=255, description="External resource ID for upsert lookup"
    )

    # Updatable fields (all optional for in-place, summary/content/type required for upsert)
    summary: str | None = Field(None, min_length=10, max_length=500, description="Updated summary")
    context_summary: str | None = Field(
        None, max_length=2000, description="Updated context summary"
    )
    content: str | None = Field(None, min_length=1, description="Updated content")
    details: dict | None = Field(None, description="Updated details (JSONB)")
    type: str | None = Field(None, min_length=1, max_length=50, description="Updated type")
    importance: float | None = Field(None, ge=0.0, le=1.0, description="Updated importance")
    tags: list[str] | None = Field(None, description="Updated tags")
    context: dict | None = Field(None, description="Updated context metadata")
    # Issue #886: change delivery_mode in place. Setting 'always' pins the
    # memory to persistent (like remember's pin-on-write); setting 'on_recall'
    # unpins it (the memory stays persistent — delivery_mode controls loading,
    # not lifecycle). None leaves it unchanged.
    delivery_mode: Literal["always", "on_recall", "on_trigger"] | None = Field(
        None, description="Updated delivery mode (always pins to persistent; on_recall unpins)"
    )

    @model_validator(mode="after")
    def validate_identifier_and_required_fields(self) -> "UpdateMemoryRequest":
        has_memory_id = self.memory_id is not None
        has_external_id = self.external_id is not None

        if not has_memory_id and not has_external_id:
            raise ValueError("Either memory_id or external_id must be provided")
        if has_memory_id and has_external_id:
            raise ValueError("Provide either memory_id or external_id, not both")

        # For upsert-by-external_id mode, enforce required fields
        if has_external_id:
            missing = [
                name
                for name, value in (
                    ("summary", self.summary),
                    ("content", self.content),
                    ("type", self.type),
                )
                if not value or (isinstance(value, str) and not value.strip())
            ]
            if missing:
                raise ValueError(
                    "summary, content, and type are required and must be non-empty "
                    "when using external_id for upsert"
                )

        return self


class UpdateMemoryResponse(BaseModel):
    """Response schema for update_memory() API."""

    status: str = "success"
    memory_id: UUID
    operation: str  # "updated" | "created" | "replaced"
    re_embedded: bool
    scope: str


class PatchMemoryRequest(BaseModel):
    """Request schema for ``PATCH /api/v1/memory/{memory_id}`` (Issue #439).

    UUID-addressed partial update. ``memory_id`` is the URL path param, not a
    body field — there is intentionally no ``external_id`` upsert mode here
    (use the existing ``update_memory`` MCP tool for that).

    All fields are optional. Only fields explicitly provided are updated;
    omitted fields preserve their current value. ``tags`` follows replace-all
    semantics (an empty list clears tags; a non-empty list replaces the
    whole list). ``scope`` and ``context_id`` are intentionally excluded —
    they have orthogonal lifecycles handled by separate operations.
    """

    summary: str | None = Field(None, min_length=10, max_length=500)
    content: str | None = Field(None, min_length=1)
    type: str | None = Field(None, min_length=1, max_length=50)
    importance: float | None = Field(None, ge=0.0, le=1.0)
    # `max_length=100` caps the tag array length; per-tag string length is
    # capped via `_validate_tag_strings` below. Tags-only patches skip the
    # MAX_CONTENT_SIZE guard (simplify deferred it to summary/content/details
    # paths only), so without these caps an authenticated workspace member
    # could either send 1M tags OR 100 tags of 1MB each to bloat the row's
    # PG ARRAY column. Both surfaces are now closed.
    tags: list[str] | None = Field(None, max_length=100)
    details: dict | None = None

    @field_validator("tags")
    @classmethod
    def _validate_tag_strings(cls, v: list[str] | None) -> list[str] | None:
        """Cap per-tag string length at 64 chars (Copilot loop 3 finding)."""
        if v is None:
            return v
        for idx, tag in enumerate(v):
            if len(tag) > 64:
                raise ValueError(
                    f"tag at index {idx} exceeds 64 chars (got {len(tag)}): '{tag[:32]}…'"
                )
        return v

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "PatchMemoryRequest":
        # `model_fields_set` is a name-only set; cheap. The previous
        # `model_dump(exclude_unset=True)` form deep-serialized the full
        # request (including `details` JSON) just to check emptiness — the
        # service layer already prefers `model_fields_set` for the same
        # reason, so the validator is now consistent with it.
        if not self.model_fields_set:
            raise ValueError("PATCH request must include at least one field to update")
        # Reject explicit `null` for fields that either map to NOT NULL DB
        # columns (`summary`/`content`/`type`/`importance`) — would 500 with a
        # PG integrity error — or would otherwise duplicate semantics with
        # field omission (`tags`: omit = preserve, `[]` = clear, `null` is
        # ambiguous). `details: null` is the only legitimate explicit-null
        # value (clears the JSONB column).
        non_nullable = ("summary", "content", "type", "importance", "tags")
        invalid_null = [
            f for f in non_nullable if f in self.model_fields_set and getattr(self, f) is None
        ]
        if invalid_null:
            raise ValueError(
                "PATCH request fields must not be null when provided: " + ", ".join(invalid_null)
            )
        return self


class ExploreRequest(BaseModel):
    """Request schema for explore() API.

    Example:
        {
            "memory_id": "uuid",
            "depth": 2,
            "relation_types": ["neural_association"],
            "min_weight": 0.05
        }
    """

    memory_id: UUID = Field(..., description="起点メモリID")
    depth: int = Field(2, ge=1, le=5, description="最大ホップ数")
    relation_types: list[str] | None = Field(None, description="フィルタするリレーションタイプ")
    min_weight: float = Field(0.05, ge=0.0, le=3.0, description="最小重み閾値（典型値: 0.02-0.05）")


class RelatedMemoryResponse(BaseModel):
    """Response schema for related memory in explore() API."""

    memory_id: UUID
    summary: str
    context_summary: str | None
    type: str
    activation: float = Field(..., description="活性化強度 (0.0-1.0)")
    hop: int = Field(..., description="起点からのホップ数")
    weight: float = Field(..., description="エッジ重み")
    path: list[UUID] = Field(..., description="起点からのパス")


class ExploreResponse(BaseModel):
    """Response schema for explore() API.

    Returns:
        seed_memory: 起点メモリ
        related_memories: 関連メモリーリスト
        metadata: 探索メタデータ
    """

    seed_memory: MemoryResponse
    related_memories: list[RelatedMemoryResponse]
    metadata: dict = Field(
        default_factory=dict,
        description="探索統計（total_activated, returned, etc.）",
    )


class MemoryStatsResponse(BaseModel):
    """Memory statistics response schema (Issue #84).

    Used by both MCP get_stats tool and REST API /memory/stats endpoint.
    """

    total_count: int = Field(..., description="Total number of memories (excluding deleted)")
    working_count: int = Field(..., description="Number of working scope memories")
    persistent_count: int = Field(..., description="Number of persistent scope memories")
    by_type: dict[str, int] = Field(
        default_factory=dict, description="Memory count breakdown by type"
    )
    by_importance: dict[str, int] = Field(
        default_factory=dict, description="Memory count breakdown by importance level"
    )
    recent_activity: int = Field(
        0, description="Recent memories count (time window varies: 24h for REST, 7d for MCP)"
    )


# ============================================================================
# Authentication Schemas
# ============================================================================


class UserResponse(BaseModel):
    """Response schema for user info."""

    email: str
    user_id: str
    name: str | None
    picture: str | None
    role: str
    timezone: str = "UTC"  # Issue #175: User timezone

    class Config:
        from_attributes = True


class UserProfileResponse(TZAwareBaseModel):
    """Response schema for user profile.

    Issue #175: User timezone settings
    Issue #221: i18n support (locale)
    Issue #514: Expose auth_method + auth_provider for sign-in-method display
    """

    id: int
    email: str
    name: str | None
    picture: str | None
    timezone: str
    locale: str
    role: str
    current_workspace_id: UUID | None
    # Issue #246: current_context_id removed (context always explicit)
    created_at: datetime
    last_login_at: datetime | None
    # Issue #514: enum-shaped — DB CHECK constraint pins
    # auth_method ∈ {oauth, password} (models/auth.py:124) and
    # auth_provider ∈ {google, github, NULL} (filled in by OAuth callback).
    # Tightening the schema layer to Literal catches accidental bad values
    # before they leave the API contract.
    auth_method: Literal["oauth", "password"]
    auth_provider: Literal["google", "github"] | None = None

    class Config:
        from_attributes = True


class UpdateUserProfileRequest(BaseModel):
    """Request schema for updating user profile.

    Issue #175: User timezone settings
    Issue #221: i18n support (locale)
    """

    name: str | None = None
    timezone: str | None = Field(
        None, description="IANA timezone (e.g., Asia/Tokyo, America/New_York)"
    )
    locale: str | None = Field(None, pattern="^(en|ja)$", description="UI language (en, ja)")


# NOTE (#991): the dead APIKeyCreate / APIKeyResponse / APIKeyCreateResponse
# schemas that used to live here were removed. They had zero callers — the live
# API-key request/response models are defined in api/routes/api_keys.py — and
# their duplicate class names produced ambiguous OpenAPI component names. The
# `MemberAPIKeyResponse` below is a distinct, live model and is unaffected.


class ExternalAPIKeyCreate(BaseModel):
    """Request schema for external API key creation."""

    key_name: str = Field(..., min_length=1, max_length=100)
    provider: str = Field(..., min_length=1, max_length=50)
    api_key_value: str = Field(..., min_length=1, description="API key value (will be encrypted)")


class ExternalAPIKeyResponse(TZAwareBaseModel):
    """Response schema for external API key (masked)."""

    id: int
    key_name: str
    provider: str
    masked_value: str = Field(..., description="Masked API key (e.g., 'sk-proj-***')")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Context Search Config Schemas (Issue #130 → #160)
# ============================================================================


class ContextSearchConfigResponse(TZAwareBaseModel):
    """Response schema for context search configuration.

    Issue #130: Context-scoped Search & Reranker Settings
    Issue #146: Added immutable embedding configuration fields
    Issue #160: Renamed from Project to Context
    """

    context_id: UUID
    semantic_weight: float = Field(
        ..., ge=0.0, le=1.0, description="Semantic search weight (0.0-1.0)"
    )
    bm25_weight: float = Field(
        ..., ge=0.0, le=1.0, description="BM25 keyword search weight (0.0-1.0)"
    )
    fetch_factor: int = Field(..., ge=1, le=10, description="Candidate retrieval multiplier (1-10)")
    use_rerank: bool = Field(..., description="Enable/disable reranking")
    reranker_provider: str = Field(..., description="Reranker provider (voyage/cohere)")
    reranker_model: str = Field(..., description="Provider-specific model name")
    embedding_model: str = Field(..., description="Embedding model (immutable)")
    embedding_dimensions: int = Field(..., description="Vector dimensions (immutable)")
    # Issue #1048: surfaced so GET reflects what update_search_config set.
    # Issue #1207: default aligned with the ORM default (new contexts start ON);
    # inert in practice — from_attributes always populates from the row.
    reinforce_enabled: bool = Field(
        default=True, description="Bounded adoption+feedback recall re-rank enabled"
    )
    reinforce_max_boost: float = Field(
        default=0.15, description="Bound on the reinforce adjustment (factor in [1-b, 1+b])"
    )
    # Issue #1065: forge-resistant mode — only host-arbitrated feedback moves ranking.
    reinforce_require_host_arbitration: bool = Field(
        default=False, description="Count only host-arbitrated (provenance='host') feedback"
    )
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ContextSearchConfigUpdate(BaseModel):
    """Request schema for updating context search configuration.

    Issue #160: Renamed from ProjectSearchConfigUpdate to ContextSearchConfigUpdate
    """

    semantic_weight: float = Field(..., ge=0.0, le=1.0, description="Semantic search weight")
    bm25_weight: float = Field(..., ge=0.0, le=1.0, description="BM25 keyword search weight")
    fetch_factor: int = Field(..., ge=1, le=10, description="Candidate retrieval multiplier")
    use_rerank: bool = Field(..., description="Enable/disable reranking")
    reranker_provider: str = Field(
        ..., description="Reranker provider: 'voyage', 'cohere', or 'self_hosted'"
    )
    reranker_model: str = Field(..., description="Provider-specific model name")
    # Issue #1048: optional (default-preserving) so existing REST callers that omit
    # them are unaffected; the MCP handler always round-trips current values.
    # Issue #1207: the repository applies model_dump(exclude_unset=True), so these
    # defaults are never written on partial updates — an omitted field can never
    # flip an explicit opt-out. Default aligned with the new ORM default anyway
    # to avoid confusion (pinned by tests/test_reinforce_default_on.py).
    reinforce_enabled: bool = Field(
        default=True,
        description="Enable the bounded adoption+feedback recall re-rank",
    )
    reinforce_max_boost: float = Field(
        default=0.15,
        ge=0.0,
        le=0.5,
        description="Bound on the reinforce adjustment (factor stays in [1-b, 1+b])",
    )
    reinforce_require_host_arbitration: bool = Field(
        default=False,
        description="Forge-resistant mode — only host-arbitrated feedback "
        "(provenance='host') moves ranking; an untrusted agent's self-feedback is ignored",
    )

    @model_validator(mode="after")
    def validate_weights_sum(self) -> "ContextSearchConfigUpdate":
        """Validate that weights sum to 1.0 (with tolerance)."""
        semantic = self.semantic_weight
        bm25 = self.bm25_weight

        if abs(semantic + bm25 - 1.0) >= 0.01:
            raise ValueError(f"Weights must sum to 1.0 (got {semantic + bm25:.2f})")
        return self

    @field_validator("reranker_model")
    @classmethod
    def validate_model_provider_match(cls, v: str, info) -> str:
        """Validate that model matches provider."""
        provider = info.data.get("reranker_provider")

        valid_models = {
            "voyage": ["rerank-2", "rerank-2-lite"],
            "cohere": ["rerank-multilingual-v3.0", "rerank-english-v3.0"],
        }

        # Self-hosted backends accept any model name (user-configured)
        if provider == "self_hosted":
            return v

        if provider and v not in valid_models.get(provider, []):
            raise ValueError(
                f"Invalid model '{v}' for provider '{provider}'. "
                f"Valid models: {valid_models.get(provider, [])}"
            )
        return v

    class Config:
        validate_assignment = True


# ============================================================================
# System Admin Management Schemas (Issue #166)
# ============================================================================


class UserWithAdminFlag(TZAwareBaseModel):
    """User model with system admin flags for admin management.

    Issue #166: System Admin vs Workspace Admin RBAC separation.
    """

    id: int
    email: str
    user_id: str
    name: str | None
    picture: str | None
    role: str  # 'admin' or 'user'
    is_initial_admin: bool
    created_at: datetime
    last_login_at: datetime | None
    memory_count: int
    is_active: bool

    model_config = {"from_attributes": True}


class SystemAdminListResponse(BaseModel):
    """Response for listing system administrators.

    Issue #166: System Admin management API.
    """

    admins: list[UserWithAdminFlag]
    total: int
    initial_admin_id: int


class PromoteToSystemAdminRequest(BaseModel):
    """Request to promote user to system admin.

    Issue #166: System Admin promotion.
    """

    user_id: str = Field(..., description="OAuth2 user_id to promote")


class PromoteToSystemAdminResponse(BaseModel):
    """Response for system admin promotion.

    Issue #166: System Admin promotion result.
    """

    success: bool
    user: UserWithAdminFlag
    message: str


# ============================================================================
# User Management Extension Schemas (Issue #164)
# ============================================================================


class UserWorkspaceInfo(TZAwareBaseModel):
    """Workspace membership info for a user.

    Issue #164: User Management拡張.
    Issue #276: Slug removed.
    """

    workspace_id: str
    workspace_name: str
    role: str  # owner/admin/member/viewer
    is_primary: bool  # Matches user.current_workspace_id
    joined_at: datetime | None
    plan_name: str  # Workspace's current plan tier

    class Config:
        from_attributes = True


class UserAccessibleContext(TZAwareBaseModel):
    """Context accessible to user.

    Issue #164: User detail - accessible contexts.
    """

    context_id: str
    context_name: str
    workspace_id: str
    workspace_name: str
    # #699: workspace owner/admin without ContextMember are reported with their
    # workspace role; explicit ContextMember rows use ContextRole.
    role: WorkspaceRole | ContextRole
    last_used_at: datetime | None

    class Config:
        from_attributes = True


class OwnedWorkspaceInfo(BaseModel):
    """Single owned workspace row for the admin workspace_summary block (#676).

    Distinct from ``UserWorkspaceInfo`` (#164): that shape carries the user's
    *membership* role (owner/admin/member/viewer) across all workspaces they
    can see. ``OwnedWorkspaceInfo`` is the projection used by the admin slot
    bonus UI — owned workspaces only (Workspace.owner_user_id), no role/
    joined_at fields, since the admin lens does not care about membership
    role when counting against the per-user cap.
    """

    id: str
    name: str
    plan_name: str  # Workspace's current plan tier (free/basic/pro)


class WorkspaceSummary(BaseModel):
    """Per-user workspace capacity summary for admin user detail (#676).

    Mirrors the fields surfaced in the "Workspace Capacity" admin UI section:
    base_cap (always 1 today, sourced from ``plan_resolver.BASE_CAP`` so the
    frontend does not hardcode), the configurable bonus, the effective cap,
    and the list of currently-owned workspaces. ``is_at_cap`` is precomputed
    so the badge variant in the UI does not have to recompute the comparison.
    """

    owned_count: int
    workspace_slot_bonus: int
    base_cap: int
    cap: int
    is_at_cap: bool
    owned_workspaces: list[OwnedWorkspaceInfo]


class UserDetailResponse(BaseModel):
    """Comprehensive user detail response.

    Issue #164: User detail page.
    Issue #676: Adds ``workspace_summary`` for the admin slot-bonus UI.
    """

    user: dict  # Basic user info
    workspaces: list[UserWorkspaceInfo]
    accessible_contexts: list[UserAccessibleContext]
    stats: dict  # Usage statistics
    workspace_summary: WorkspaceSummary | None = None  # #676

    class Config:
        from_attributes = True


class UpdateWorkspaceSlotBonusRequest(BaseModel):
    """Body for PATCH /admin/users/{user_id}/workspace_slot_bonus (#676).

    ``delta`` is the signed increment to apply atomically. The ±1M bound is a
    sanity limit (frontend only sends ±1; the cap protects against admin
    typos and against INT32 overflow on `workspace_slot_bonus + delta`).
    ``reason`` is required only when the resulting cap would fall below the
    user's current owned_count (a destructive admin operation); otherwise it
    may be ``None``. The 500-char cap matches the audit_log payload budget.
    """

    delta: int = Field(
        ...,
        ge=-1_000_000,
        le=1_000_000,
        description="Signed increment (±1M sanity bound; frontend uses ±1).",
    )
    reason: str | None = Field(
        None,
        max_length=500,
        description="Free-form reason (required for destructive over-cap ops).",
    )


class UpdateWorkspaceSlotBonusResponse(BaseModel):
    """Response for PATCH /admin/users/{user_id}/workspace_slot_bonus (#676).

    Returns enough state for the frontend to update its local view without a
    refetch round-trip: the bonus before/after, plus the recomputed cap
    summary. ``reason`` is echoed back so the optimistic UI can include it in
    the success toast.
    """

    before_value: int
    after_value: int
    owned_count: int
    base_cap: int
    cap: int
    is_at_cap: bool
    reason: str | None


# ============================================================================
# Workspace Invitation Schemas (Issue #165)
# ============================================================================


class WorkspaceInvitationCreate(BaseModel):
    """Create workspace invitation request.

    Issue #165: Team Collaboration - Workspace Invitation System

    Note: Email is required and must match the Google account used for OAuth login.
    """

    email: str = Field(
        ...,
        description="Email address (must match Google OAuth account)",
        max_length=255,
        pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    )
    role: WorkspaceRole = Field(
        WorkspaceRole.MEMBER,
        description="Role to assign upon acceptance (owner is not invitable — #1166)",
    )
    expires_in_days: int | None = Field(
        None,
        ge=1,
        le=365,
        description="Days until expiration (7/30/90/365 or null=never)",
    )
    allowed_context_ids: list[str] | None = Field(
        None,
        description="Context IDs to allow (required for member/viewer, minimum 1)",
    )

    @field_validator("role")
    @classmethod
    def _reject_owner_role(cls, v: WorkspaceRole) -> WorkspaceRole:
        """Issue #1166: invitations must never grant the owner role.

        The sanctioned owner-change path is the ownership transfer flow;
        rejecting here surfaces a 422 before any route/service code runs
        (the service re-checks as defense in depth for non-HTTP callers).
        """
        if v == WorkspaceRole.OWNER:
            raise ValueError(
                "role=owner invitations are not supported; use the ownership transfer flow instead"
            )
        return v


class WorkspaceInvitationResponse(TZAwareBaseModel):
    """Workspace invitation response.

    Issue #165: Team Collaboration - Workspace Invitation System
    """

    id: int
    workspace_id: UUID
    # Issue #1164: token / invitation_url are bearer join-credentials. They are
    # populated in the POST create response and in session-principal list
    # responses, but OMITTED (None) for programmatic (API-key) list responses so
    # CI logs never become a workspace-join credential dump.
    token: str | None
    email: str | None
    role: str
    invited_by: str
    expires_at: datetime | None
    accepted_at: datetime | None
    accepted_by: str | None
    created_at: datetime
    invitation_url: str | None  # Full URL to accept invitation (None when omitted)
    is_expired: bool
    is_accepted: bool
    allowed_context_ids: list[str] | None = Field(
        None,
        description="Allowed context IDs for member/viewer roles (Migration 042)",
    )

    class Config:
        from_attributes = True


class AcceptInvitationRequest(BaseModel):
    """Accept invitation request.

    Issue #165: Team Collaboration - Workspace Invitation System
    """

    token: str = Field(..., min_length=20, description="Invitation token")


class AcceptInvitationResponse(BaseModel):
    """Accept invitation response.

    Issue #165: Team Collaboration - Workspace Invitation System
    """

    success: bool
    workspace: dict  # WorkspaceResponse
    member: dict  # WorkspaceMemberResponse


# ============================================================================
# Workspace OpenAI Key Status (Issue #181)
# ============================================================================


class OpenAIKeyStatusResponse(BaseModel):
    """OpenAI API key status for workspace.

    Issue #181: OpenAI API key guidance in context creation.
    """

    has_key: bool = Field(..., description="Whether workspace has OpenAI API key")
    can_configure: bool = Field(..., description="Whether current user can configure keys")
    external_keys_url: str = Field(..., description="URL to external keys configuration page")


# ============================================================================
# Pending Invitations (Issue #179)
# ============================================================================


class PendingInvitationItem(TZAwareBaseModel):
    """Pending invitation item for current user.

    Issue #179: In-app invitation notifications.
    """

    id: int
    workspace_id: str
    workspace_name: str
    role: str
    invited_by: str
    expires_at: datetime | None
    created_at: datetime
    token: str
    invitation_url: str

    class Config:
        from_attributes = True


class PendingInvitationsResponse(BaseModel):
    """Response for pending invitations query.

    Issue #179: In-app invitation notifications.
    """

    pending_invitations: list[PendingInvitationItem]
    count: int


# ============================================================================
# Member Credentials (Migration 034)
# ============================================================================


class MemberAPIKeyResponse(BaseModel):
    """Response for member's API key (Zero-knowledge model).

    Migration 034: Member-scoped credentials.
    Issue #626: Optional public-context binding.
    """

    id: int
    name: str
    key_prefix: str
    plaintext_key: str | None  # Only if visible + owner
    is_visible: bool
    visibility_expires_at: str | None
    created_at: str
    last_used_at: str | None = None  # Issue #943: "Last used" column
    revoked_at: str | None
    # Issue #1165: owner-provisioned keys REQUIRE expires_days, so the resulting
    # expiry must be observable (it was write-only before v0.42). None = never
    # expires (session self-mint).
    expires_at: str | None = None
    bound_context_id: str | None = None  # Issue #626: public attribution


class MemberOAuthAppResponse(BaseModel):
    """Response for member's OAuth app (Zero-knowledge model).

    Migration 034: Member-scoped credentials.
    """

    client_id: str
    client_name: str
    plaintext_secret: str | None  # Only if visible + owner
    is_visible: bool
    visibility_expires_at: str | None
    created_at: str
    redirect_uris: list[str]
    scope: str


class MemberCredentialsResponse(BaseModel):
    """Response for member credentials (API Keys).

    Migration 034: Member-scoped credentials.
    Note: OAuth Apps managed via /oauth/clients API.
    """

    api_keys: list[MemberAPIKeyResponse]  # Multiple API keys support
    target_user_role: str  # Target user's workspace role (for permission checks)


class CreateAPIKeyRequest(BaseModel):
    """Request for creating a new API key.

    Issue #626: ``bound_context_id`` makes this a public-bound key that is
    attributed to one ``is_public=true`` context (immutable binding —
    revoke and re-create to change). Mutually exclusive with the
    workspace-scoping derived from the URL: when supplied, the key is
    stored with ``workspace_id=NULL``.
    """

    name: str
    auto_hide_minutes: int = 10  # Auto-hide after 10 minutes (default)
    bound_context_id: str | None = None  # Issue #626
    # Issue #1165: owner-provisioned mints REQUIRE an expiry (the route enforces
    # 400 if omitted for the programmatic path); session self-mint leaves it None
    # (unchanged — no expiry, as today). 1–3650 days mirrors /api/v1/config/api-keys.
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class RegenerateAPIKeyResponse(BaseModel):
    """Response for API key regeneration.

    Migration 034: Returns new plaintext key (shown once).
    """

    key: str
    key_prefix: str
    key_id: int


class RegenerateOAuthSecretResponse(BaseModel):
    """Response for OAuth client secret regeneration.

    Migration 034: Returns new plaintext secret (shown once).
    """

    client_secret: str
    client_id: str


# ============================================================================
# Resource Ingest API Schemas (Issue #238)
# ============================================================================


class ResourceEventRequest(BaseModel):
    """Request schema for resource event ingestion.

    Issue #238: Resource-driven incremental indexing.

    Example:
        {
            "op": "upsert",
            "doc_id": "PROD-12345",
            "version": 3,
            "payload": {
                "product_name": "ワイヤレスイヤホン",
                "price": 5980,
                "category": "オーディオ"
            },
            "idempotency_key": "unique-client-key-123"
        }
    """

    op: str = Field(
        ..., pattern=r"^(upsert|delete)$", description="Operation: 'upsert' or 'delete'"
    )
    doc_id: str = Field(
        ..., min_length=1, max_length=255, description="Document ID (stable across versions)"
    )
    version: int | None = Field(
        None,
        ge=1,
        description="Document version (NULL for delete-all-versions, >=1 for specific version)",
    )  # Issue #262
    payload: dict | None = Field(None, description="Document payload (NULL for delete)")
    idempotency_key: str | None = Field(
        None, min_length=1, max_length=255, description="Optional idempotency key for deduplication"
    )
    event_metadata: dict = Field(
        default_factory=dict, description="Additional metadata (source, tenant, etc.)"
    )
    importance: float | None = Field(
        None, ge=0.0, le=1.0, description="Memory importance score (0.0-1.0, default 0.6)"
    )  # Issue #262

    @model_validator(mode="after")
    def validate_payload_for_operation(self):
        """Validate payload and version based on operation type."""
        if self.op == "upsert":
            if not self.payload:
                # Code quality: Improved error message with context
                raise ValueError(
                    f"payload is required for upsert operation (doc_id={self.doc_id}, "
                    f"version={self.version}, current_payload={self.payload})"
                )
            # P0-3: Fix - use 'is None' instead of 'not self.version' to allow version=0
            if self.version is None:
                # Code quality: Improved error message with context
                raise ValueError(
                    f"version is required for upsert operation (doc_id={self.doc_id}, "
                    f"current_version={self.version})"
                )
        if self.op == "delete" and self.payload:
            # Code quality: Improved error message with context
            raise ValueError(
                f"payload must be null for delete operation (doc_id={self.doc_id}, "
                f"current_payload_keys={list(self.payload.keys()) if self.payload else None})"
            )
        return self


class ResourceEventBatchRequest(BaseModel):
    """Request schema for batch resource event ingestion.

    Issue #238: Batch upsert/delete for efficiency.

    Example:
        {
            "events": [
                {"op": "upsert", "doc_id": "PROD-1", "version": 1, "payload": {...}},
                {"op": "upsert", "doc_id": "PROD-2", "version": 1, "payload": {...}},
                {"op": "delete", "doc_id": "PROD-999", "version": 5}
            ]
        }
    """

    events: list[ResourceEventRequest] = Field(
        ..., min_length=1, max_length=100, description="Events (max 100)"
    )


class ResourceEventResponse(BaseModel):
    """Response schema for resource event ingestion."""

    status: str = "success"
    event_id: int = Field(..., description="Created event ID")
    queued: bool = Field(True, description="Whether indexing is queued")
    # Bugfix: Allow None for unknown estimation
    estimated_indexing_time_seconds: int | None = Field(
        None, description="Estimated time until indexed (seconds, None if unknown)"
    )


class ResourceEventBatchResponse(BaseModel):
    """Response schema for batch resource event ingestion."""

    status: str = "success"
    created_count: int = Field(..., description="Number of events created")
    failed_count: int = Field(0, description="Number of events that failed")
    event_ids: list[int] = Field(default_factory=list, description="Created event IDs")
    errors: list[dict] = Field(default_factory=list, description="Error details for failed events")


class PiiGuardrailConfig(BaseModel):
    """Connector PII-scrubbing config consumed by the ai-worker pre-compile stage.

    Issue #866 (F6-d follow-up). memory-cloud stores this opaquely as JSONB on
    ``workspace_connectors.pii_guardrail_config`` but validates the shape at the
    provision path so a malformed config (typo'd/unknown key, missing detectors)
    is rejected loudly instead of silently accepted — a Fail-Secure guardrail.
    The agreed schema is the contract in ``docs/pii-guardrail-consumption-contract.md``.

    ``extra="forbid"`` is deliberate: this is *request* validation, so an unknown
    key (e.g. ``detector`` vs ``detectors``) must fail rather than be ignored
    (silently-ignore would be fail-open). This is the opposite default from
    response models, which use ``extra="ignore"`` for forward-compat.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(..., description="Master switch for worker-side scrubbing.")
    detectors: list[str] = Field(
        default_factory=list,
        description="Presidio-style recognizer names; non-empty required when enabled=true.",
    )
    redaction: Literal["mask", "hash", "remove"] = Field(
        "mask", description="How a matched span is rewritten."
    )
    locale: str = Field("en", description="Recognizer locale (e.g. en / ja).")
    fail_closed: bool = Field(
        True, description="On detection error: true=drop event, false=ingest with warning."
    )

    @field_validator("locale")
    @classmethod
    def _locale_not_blank(cls, v: str) -> str:
        # An explicitly blank locale overrides the "en" default with an unusable
        # recognizer locale. Reject it (the default is not run through this
        # validator, so omitting locale still yields "en").
        if not v or not v.strip():
            raise ValueError("locale must not be blank")
        return v

    @model_validator(mode="after")
    def _require_detectors_when_enabled(self) -> "PiiGuardrailConfig":
        if self.enabled:
            if not self.detectors:
                raise ValueError("detectors must be non-empty when enabled=true")
            if any(not d or not d.strip() for d in self.detectors):
                raise ValueError("detectors must not contain blank entries when enabled=true")
        return self


def validate_pii_guardrail_config(raw: dict | None) -> dict | None:
    """Validate a raw ``pii_guardrail_config`` against :class:`PiiGuardrailConfig`.

    Shared by both provision call-sites (REST ``POST /workspace-connectors`` and the
    MCP ``setup_connector`` tool) so the documented schema has a single enforcement
    point. ``None`` (unconfigured) is accepted and passed through unchanged — the
    worker enforces fail-closed at ingest per the null asymmetry in the contract doc.

    Returns the normalized config dict (defaults materialized) ready for JSONB
    storage, or ``None``.

    Raises:
        ValueError: if ``raw`` is not a valid config. The message lists only field
            locations and reasons — never the offending input values
            (``include_input=False``), so a malformed config cannot echo sensitive
            content back to the caller.
    """
    if raw is None:
        return None
    try:
        return PiiGuardrailConfig.model_validate(raw).model_dump()
    except ValidationError as e:
        parts = []
        for err in e.errors(include_input=False):
            loc = ".".join(str(p) for p in err["loc"])
            # Model-level (@model_validator) errors carry an empty loc; render just
            # the message rather than a dangling "<empty>: msg" prefix.
            parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
        raise ValueError(f"invalid pii_guardrail_config — {'; '.join(parts)}") from e
