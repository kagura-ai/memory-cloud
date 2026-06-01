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


class MemoryResponse(TZAwareBaseModel):
    """Response schema for single memory."""

    memory_id: UUID
    summary: str
    context_summary: str | None
    type: str
    importance: float
    scope: str
    created_at: datetime
    client: str
    tags: list[str]
    context: dict | None
    score: float | None = Field(None, description="検索スコア（recall時のみ）")
    source_uri: str | None = None
    source_type: Literal["file", "url", "vault", "api", "manual"] | None = None

    class Config:
        from_attributes = True


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


class RecallResponse(BaseModel):
    """Response schema for recall() API.

    Issue #104: Added related_tags to help LLMs understand tag context.
    Issue #216: Added explore_hints for graph discovery bridging.
    """

    results: list[MemoryResponse]
    related_tags: list[RelatedTagItem] = []
    explore_hints: list[ExploreHint] | None = None


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
    source_type: Literal["file", "url", "vault", "api", "manual"] | None = None
    # Issue #440: declared_link references for the dialog "References" section.
    # Naming: outgoing_has_more / incoming_has_more matches MemoryListResponse.has_more
    # (codebase precedent for capped collections); the issue body's `*_truncated`
    # suggestion is intentionally overridden for consistency.
    outgoing_links: list[LinkedMemoryRef] = Field(default_factory=list)
    outgoing_has_more: bool = False
    incoming_links: list[LinkedMemoryRef] = Field(default_factory=list)
    incoming_has_more: bool = False


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


class APIKeyCreate(BaseModel):
    """Request schema for API key creation."""

    name: str = Field(..., min_length=1, max_length=100)
    expires_in_days: int | None = Field(None, ge=1, le=365, description="有効期限（日数）")


class APIKeyResponse(TZAwareBaseModel):
    """Response schema for API key."""

    id: int
    key_prefix: str
    name: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    expires_at: datetime | None

    class Config:
        from_attributes = True


class APIKeyCreateResponse(BaseModel):
    """Response schema for API key creation (includes plaintext key once)."""

    key: str = Field(..., description="API key (show once, never stored in plaintext)")
    key_info: APIKeyResponse


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
        ..., description="Reranker provider: 'voyage', 'cohere', or 'ollama'"
    )
    reranker_model: str = Field(..., description="Provider-specific model name")

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

        # Ollama accepts any model name (user-configured)
        if provider == "ollama":
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
        description="Role to assign upon acceptance",
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


class WorkspaceInvitationResponse(TZAwareBaseModel):
    """Workspace invitation response.

    Issue #165: Team Collaboration - Workspace Invitation System
    """

    id: int
    workspace_id: UUID
    token: str
    email: str | None
    role: str
    invited_by: str
    expires_at: datetime | None
    accepted_at: datetime | None
    accepted_by: str | None
    created_at: datetime
    invitation_url: str  # Full URL to accept invitation
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
    revoked_at: str | None
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
