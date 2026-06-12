# REST API Response Models — pre-1.0 surface enumeration

> Issue: #622 — pre-1.0 public API surface enumeration and freeze
> Enumerated at commit 20ae959a2c79cacd2cf7922512ad780f540e9c60 (main HEAD, 2026-06-12)
> Scope: all BaseModel / TZAwareBaseModel subclasses defined in backend/src/api/routes/ (46 route files, 208 model classes — 208 of 208 enumerated)

Notes:

- 36 of the 46 route files define models; the following 10 define none locally (they reuse models from other modules or return plain dicts): `__init__.py`, `attachments.py`, `connectors_slack.py`, `context_search_config.py`, `invitations.py`, `member_credentials.py`, `resource_ingest.py`, `system_admins.py`, `users.py`, `well_known.py`.
- Two route-file classes subclass an in-scope model rather than `BaseModel` directly and are therefore outside the 208-class grep, but are part of the response surface and noted inline for completeness: `Bm25DriftDetail(Bm25DriftSummary)` (bm25_drift.py L64, adds `context_deleted`, `top_divergent_terms`) and `SleepReportDetail(SleepReportSummary)` (sleep_reports.py L66, adds `context_deleted`, `embedding_calls_made`, `error_message`, and five `*_result: dict` fields).
- Field convention: `field_name: type — required|optional (default ...)`. Required = no default in the model definition (Pydantic v2 semantics: `X | None` without a default is still required).
- Request models are included for completeness and marked `(request model)`; the freeze priority per #622 is the response surface.

## admin.py

### UserInfo (TZAwareBaseModel, L58)
> User information for admin view.
- `id: str` — required
- `email: str` — required
- `name: str` — required
- `picture: str | None` — optional
- `role: str` — required
- `created_at: datetime` — required
- `last_login: datetime | None` — optional
- `memory_count: int` — required
- `is_active: bool` — required
- `timezone: str` — optional (default `'UTC'`)
- `auth_provider: str | None` — optional
- `workspaces: list[dict]` — optional (default `[]`)
- `owned_count: int` — optional (default `0`)
- `workspace_slot_bonus: int` — optional (default `0`)
- `base_cap: int` — optional (default `BASE_CAP`)
- `cap: int` — optional (default `BASE_CAP`)

### UserStats (BaseModel, L103)
> Detailed statistics for a specific user.
- `user: dict` — required
- `memories: dict` — required
- `api_usage: dict` — required

### UserListResponse (BaseModel, L111)
> Response for user list.
- `users: list[UserInfo]` — required
- `total: int` — required

### RoleUpdateRequest (BaseModel, L118) (request model)
> Request to update user role.
- `role: str` — required

### AdminErasureRequestBody (BaseModel, L1079) (request model)
> Payload for POST /admin/users/{user_id}/erase.
- `reason_code: AdminErasureReasonCode` — required
- `reason_detail: str | None` — optional

### ContextRecoveryRequest (BaseModel, L1217) (request model)
> Request to recover a deleted context from Qdrant data.
- `context_id: str` — required
- `workspace_id: str | None` — optional
- `context_name: str | None` — optional
- `dry_run: bool` — optional (default `True`)

### ContextRecoveryResponse (BaseModel, L1226)
> Result of context recovery attempt.
- `context_id: str` — required
- `workspace_id: str` — required
- `qdrant_points_found: int` — required
- `memories_recovered: int` — required
- `memories_already_existed: int` — required
- `context_record_created: bool` — required
- `search_config_restored: bool` — required
- `dry_run: bool` — required
- `errors: list[str]` — required

## admin_neural.py

### RecalibrateResponse (BaseModel, L43)
> 202 response body for the admin recalibrate endpoint.
- `accepted: bool` — required
- `model_name: str` — required
- `dimensions: int` — required

## admin_plans.py

### WorkspacePlanInfo (BaseModel, L46)
> Workspace with plan information.
- `id: str` — required
- `name: str` — required
- `plan_name: str` — required
- `owner_user_id: str` — required
- `owner_name: str | None` — optional
- `owner_email: str | None` — optional
- `total_memories: int` — required
- `memory_limit: int` — required
- `mcp_calls_per_day: int` — required
- `mcp_calls_per_week: int` — required

### UpdatePlanRequest (BaseModel, L64) (request model)
> Request to update workspace plan tier.
- `plan_name: str` — required
- `reason: str | None` — optional

### QuotaBreakdown (BaseModel, L71)
> Quota values for a single tier (Issue #665).
- `memory_limit: int` — required
- `mcp_calls_per_day: int` — required
- `max_contexts: int` — required
- `max_members: int` — required
- `analysis_runs_per_day: int` — required
- `rest_calls_per_day: int` — optional (default `0`)
- `public_calls_per_day: int` — optional (default `0`)
- `storage_bytes_limit: int` — optional (default `0`)
- `sleep_enabled_contexts_limit: int` — optional (default `0`)
- `max_resource_tokens: int` — optional (default `0`)
- `max_connectors: int` — optional (default `0`)

### AddonValues (BaseModel, L97)
> Addon bonus values.
- `memory_bonus: int` — optional (default `0`)
- `mcp_quota_bonus: int` — optional (default `0`)
- `rest_quota_bonus: int` — optional (default `0`)
- `public_quota_bonus: int` — optional (default `0`)
- `member_bonus: int` — optional (default `0`)
- `context_bonus: int` — optional (default `0`)
- `analysis_bonus: int` — optional (default `0`)
- `storage_bonus_mb: int` — optional (default `0`)
- `sleep_contexts_bonus: int` — optional (default `0`)
- `connector_bonus: int` — optional (default `0`)

### UsageValues (BaseModel, L117)
> Current usage values.
- `memories: int` — required
- `contexts: int` — required
- `members: int` — required

### SpendCapValues (BaseModel, L125)
> BYOK embedding spend cap values (Issue #709).
- `tier_default_daily_usd: float | None` — optional
- `tier_default_monthly_usd: float | None` — optional
- `override_daily_usd: float | None` — optional
- `override_monthly_usd: float | None` — optional
- `effective_daily_usd: float | None` — optional
- `effective_monthly_usd: float | None` — optional
- `current_daily_usd: float` — optional (default `0.0`)
- `current_monthly_usd: float` — optional (default `0.0`)

### WorkspaceQuotaDetail (BaseModel, L144)
> Detailed quota breakdown for a workspace.
- `workspace_id: str` — required
- `workspace_name: str` — required
- `plan_name: str` — required
- `base: QuotaBreakdown` — required
- `addon: AddonValues` — required
- `effective: QuotaBreakdown` — required
- `usage: UsageValues` — required
- `spend_cap: SpendCapValues | None` — optional

### UpdateAddonRequest (BaseModel, L157) (request model)
> Request to update workspace addon bonuses (Issue #665).
- `addon_memory_bonus: int | None` — optional
- `addon_mcp_quota_bonus: int | None` — optional
- `addon_member_bonus: int | None` — optional
- `addon_context_bonus: int | None` — optional
- `addon_analysis_bonus: int | None` — optional
- `addon_rest_quota_bonus: int | None` — optional
- `addon_public_quota_bonus: int | None` — optional
- `addon_storage_bonus_mb: int | None` — optional
- `addon_sleep_contexts_bonus: int | None` — optional
- `addon_connector_bonus: int | None` — optional

### UpdateSpendCapRequest (BaseModel, L194) (request model)
> Request to update the per-workspace embedding spend cap (Issue #709).
- `embedding_daily_cap_usd: float | None` — optional
- `embedding_monthly_cap_usd: float | None` — optional

### PlanChangeAuditEntry (BaseModel, L212)
> Plan change audit log entry.
- `id: int` — required
- `workspace_id: str` — required
- `workspace_name: str` — required
- `old_plan: str | None` — required
- `new_plan: str` — required
- `changed_by: str` — required
- `changed_at: str` — required
- `reason: str | None` — required

### AddonCacheDriftEntry (BaseModel, L225)
> One drifted addon cache column for a workspace (Issue #799).
- `workspace_id: str` — required
- `workspace_name: str` — required
- `addon_type: str` — required
- `cache_column: str` — required
- `cache_value: int` — required
- `expected_value: int` — required

### PlanTierInfo (BaseModel, L242)
> Plan tier configuration served to the admin tiers comparison table.
- `name: str` — required
- `display_name: str` — required
- `price_monthly: int` — required
- `max_contexts_per_workspace: int` — required
- `max_members_per_workspace: int` — required
- `max_resource_tokens: int` — required
- `memory_limit: int` — required
- `mcp_calls_per_day: int` — required
- `mcp_calls_per_week: int` — required
- `rest_calls_per_day: int` — required
- `rest_calls_per_week: int` — required
- `public_calls_per_day: int` — required
- `public_calls_per_week: int` — required
- `bound_public_calls_per_minute: int` — required
- `analysis_runs_per_day: int` — required
- `storage_limit_bytes: int` — required
- `sleep_enabled_contexts_limit: int` — required
- `embedding_daily_cap_usd: float | None` — optional
- `embedding_monthly_cap_usd: float | None` — optional
- `allows_shared_contexts: bool` — required
- `features: list[str]` — required

## admin_signup_gate.py

### SignupGateConfigResponse (BaseModel, L38)
> Config read-model.
- `enabled: bool` — required
- `mode: Literal['manual', 'github_sponsors', 'both']` — required
- `github_sponsors_grace_period_days: int` — required

### SignupGateConfigUpdate (BaseModel, L53) (request model)
- `enabled: bool` — required
- `mode: Literal['manual']` — required

### AllowlistEntryResponse (TZAwareBaseModel, L62)
> Allowlist entry read-model.
- `id: UUID` — required
- `provider: str` — required
- `subject_id: str` — required
- `subject_label: str` — required
- `github_user_id: str` — required
- `github_username: str` — required
- `source: str` — required
- `state: str` — required
- `added_by_user_id: str | None` — required
- `created_at: datetime` — required

### AllowlistAddRequest (BaseModel, L89) (request model)
> Allowlist add payload (provider-aware since #655).
- `provider: Literal['github', 'google']` — optional (default `'github'`)
- `github_username: str | None` — optional
- `email: str | None` — optional

## admin_sleep.py

### SleepRunRequest (BaseModel, L42) (request model)
> Body for POST /admin/sleep/run.
- `context_id: UUID | None` — optional

### SleepRunResponse (BaseModel, L53)
> 202 response for POST /admin/sleep/run.
- `report_ids: list[UUID]` — required

## agent_state.py

### AgentStateSetRequest (BaseModel, L50) (request model)
> Body for upserting an agent-state entry.
- `value: Any` — required
- `ttl_seconds: int | None` — optional

### AgentStateKeyResponse (BaseModel, L69)
> Envelope for a write that returns just the affected key (set / delete).
- `key: str` — required

### AgentStateValueResponse (BaseModel, L75)
> Envelope for a single-key read.
- `key: str` — required
- `value: Any` — required

### AgentStateListResponse (BaseModel, L82)
> Envelope for listing all live entries in a context.
- `states: dict[str, Any]` — required
- `count: int` — required

## analyses.py

### AnalysisPreviewRequest (BaseModel, L102) (request model)
> Pre-flight cost-estimate request body.
- `from_dt: str | None` — optional
- `to_dt: str | None` — optional
- `types: list[str] | None` — optional
- `tags: list[str] | None` — optional
- `min_importance: float | None` — optional
- `query: str | None` — optional
- `model_id: int | None` — optional

### AnalysisPreviewResponse (BaseModel, L149)
> Cost-estimate output (Stage [A] from preview.py).
- `memory_count: int` — required
- `cluster_count_estimate: int` — required
- `estimated_cost_cents: int` — required
- `model_id: str` — required
- `breakdown: dict[str, int]` — required

### AnalysisStartResponse (TZAwareBaseModel, L163)
> 202 Accepted body.
- `run_id: UUID` — required
- `status: str` — required
- `started_at: datetime` — required

### AnalysisRow (TZAwareBaseModel, L180)
> One row in list / single-fetch responses.
- `run_id: UUID` — required
- `workspace_id: UUID` — required
- `context_id: UUID` — required
- `status: str` — required
- `triggered_by: str` — required
- `started_at: datetime` — required
- `finished_at: datetime | None` — required
- `input_count: int` — required
- `cost_estimated_cents: int | None` — required
- `cost_actual_cents: int | None` — required
- `error: str | None` — required
- `cancellation_reason: str | None` — required

### AnalysisListResponse (BaseModel, L209)
> Paginated list of runs.
- `items: list[AnalysisRow]` — required
- `next_cursor: str | None` — required

### ClusterRow (BaseModel, L216)
> One cluster within an analysis run.
- `cluster_index: int` — required
- `label: str` — required
- `description: str | None` — required
- `count: int` — required
- `centroid_2d: list[float]` — required
- `representative_memory_ids: list[UUID]` — required
- `property_stats: dict[str, Any]` — required
- `label_confidence: float` — required

### ClusterListResponse (BaseModel, L244)
> All clusters for a run, ordered by ``cluster_index``.
- `items: list[ClusterRow]` — required

### PositionRow (BaseModel, L250)
> One ``(memory_id, x, y, cluster_index)`` row for scatter rendering.
- `memory_id: UUID` — required
- `x: float` — required
- `y: float` — required
- `cluster_index: int` — required

### PositionListResponse (BaseModel, L259)
> All scatter positions for a run, ordered by ``cluster_index``.
- `items: list[PositionRow]` — required

### AnalysisCancelResponse (TZAwareBaseModel, L265)
> DELETE /{run_id} response — confirms the soft-cancel.
- `run_id: UUID` — required
- `status: str` — required
- `cancellation_reason: str | None` — required
- `finished_at: datetime | None` — required

## api_keys.py

### APIKeyCreate (BaseModel, L54) (request model)
> Request model for creating an API key.
- `name: str` — required
- `expires_days: int | None` — optional

### DailyStats (BaseModel, L68)
> Daily usage statistics.
- `date: str` — required
- `count: int` — required

### APIKeyStats (BaseModel, L75)
> API key usage statistics.
- `total_requests: int` — required
- `daily_stats: list[DailyStats]` — required
- `period_start: str` — required
- `period_end: str` — required

### APIKeyResponse (TZAwareBaseModel, L84)
> Response model for API key metadata.
- `id: int` — required ⚠ sequential integer DB PK used as the public identifier (leaks row count; consider opaque id before freeze)
- `key_prefix: str` — required
- `name: str` — required
- `user_id: str` — required
- `created_at: datetime` — required
- `last_used_at: datetime | None` — optional
- `revoked_at: datetime | None` — optional
- `expires_at: datetime | None` — optional
- `status: Literal['active', 'revoked', 'expired']` — required

## auth.py

### LoginResponse (BaseModel, L92)
> OAuth2 login response.
- `authorization_url: str` — required
- `state: str` — required

### CallbackResponse (BaseModel, L99)
> OAuth2 callback response.
- `success: bool` — required
- `user_id: str` — required
- `email: str` — required
- `role: str` — required
- `message: str` — required

### ProviderInfo (BaseModel, L445)
> OAuth provider info.
- `name: str` — required

### ProvidersResponse (BaseModel, L451)
> Available OAuth providers.
- `providers: list[ProviderInfo]` — required

### PasswordLoginRequest (BaseModel, L1293) (request model)
> Password login request.
- `login_id: str` — required
- `password: str` — required

### PasswordLoginResponse (BaseModel, L1300)
> Password login response.
- `success: bool` — required
- `mfa_required: bool` — optional (default `False`)
- `mfa_session_token: str | None` — optional
- `redirect_url: str | None` — optional

### MfaVerifyRequest (BaseModel, L1309) (request model)
> MFA verification request.
- `mfa_session_token: str` — required
- `totp_code: str` — required

### AuthConfigResponse (BaseModel, L1316)
> Auth configuration for frontend.
- `password_login_enabled: bool` — required
- `google_oauth_enabled: bool` — required
- `github_oauth_enabled: bool` — required

## bm25_drift.py

### Bm25DriftSummary (TZAwareBaseModel, L50)
> Drift log row, list-view shape (no top_divergent_terms).
- `id: int` — required
- `context_id: UUID` — required
- `context_name: str | None` — optional
- `measured_at: datetime` — required
- `psi: float | None` — required
- `psi_status: str` — required
- `m_memory_points: int` — required
- `r_resource_points: int` — required
- `num_terms: int` — required

### Bm25DriftListResponse (BaseModel, L71)
- `rows: list[Bm25DriftSummary]` — required
- `total: int` — required
- `limit: int` — required
- `offset: int` — required

### Bm25DriftDetailResponse (BaseModel, L78)
- `row: Bm25DriftDetail` — required (`Bm25DriftDetail(Bm25DriftSummary)` L64 adds `context_deleted: bool = False`, `top_divergent_terms: list[dict] | None`)

### Bm25DriftRunRequest (BaseModel, L82) (request model)
> Body for POST /admin/bm25-drift/run.
- `context_id: UUID | None` — optional

### Bm25DriftRunResponse (BaseModel, L94)
- `scheduled_context_count: int` — required

### Bm25DriftRevealRequest (BaseModel, L98) (request model)
> Body for POST /admin/bm25-drift/{row_id}/reveal-terms.
- `reason: str` — required (`Field(..., min_length=10)`, justification is audit-logged)

### ResolvedTermEntry (BaseModel, L108)
> Single entry from top_divergent_terms with resolved token.
- `index: int` — required
- `df_memory: float` — required
- `df_global: float` — required
- `idf_memory: float` — required
- `idf_global: float` — required
- `delta: float` — required
- `token: str | None` — optional (plaintext term — admin-gated reveal endpoint with audit logging, by design)

## config.py

### ConfigValue (BaseModel, L28)
> Single configuration value.
- `key: str` — required
- `value: Any` — required
- `category: str` — required
- `description: str | None` — optional
- `is_sensitive: bool` — optional (default `False`)

### ConfigListResponse (BaseModel, L38)
> Configuration list response.
- `configs: list[ConfigValue]` — required
- `total: int` — required

### ConfigUpdateRequest (BaseModel, L45) (request model)
> Update configuration value.
- `value: Any` — required

### ConfigBatchRequest (BaseModel, L51) (request model)
> Batch update configuration.
- `updates: dict[str, Any]` — required

### ConfigValidateRequest (BaseModel, L57) (request model)
> Validate configuration.
- `key: str` — required
- `value: Any` — required

### ConfigKeySchema (BaseModel, L64)
> Configuration key metadata schema.
- `key: str` — required
- `type: str` — required
- `category: str` — required
- `description: str` — required
- `default_value: Any` — required
- `enum_values: list[str] | None` — optional
- `enum_descriptions: dict[str, str] | None` — optional
- `min_value: float | None` — optional
- `max_value: float | None` — optional
- `is_sensitive: bool` — optional (default `False`)
- `requires_restart: bool` — optional (default `False`)
- `impact: str | None` — optional
- `examples: list[str] | None` — optional
- `recommended: str | None` — optional
- `documentation_url: str | None` — optional

## contexts.py

### ContextCreate (BaseModel, L108) (request model)
> Request model for creating a context.
- `name: str` — required
- `display_name: str | None` — optional
- `description: str | None` — optional
- `summary: str | None` — optional
- `usage_guide: str | None` — optional
- `embedding_model: str | None` — optional
- `is_private: bool` — optional (default `True`)

### ContextUpdate (BaseModel, L151) (request model)
> Request model for updating a context.
- `display_name: str | None` — optional
- `description: str | None` — optional
- `summary: str | None` — optional
- `usage_guide: str | None` — optional
- `is_private: bool | None` — optional
- `is_public: bool | None` — optional
- `resource_id: str | None` — optional
- `is_locked: bool | None` — optional
- `sleep_mode: SleepMode | None` — optional

### ContextResponse (TZAwareBaseModel, L198)
> Response model for context data.
- `id: UUID` — required
- `name: str` — required
- `display_name: str | None` — optional
- `description: str | None` — optional
- `summary: str | None` — optional
- `usage_guide: str | None` — optional
- `is_default: bool` — required
- `is_private: bool` — optional (default `True`)
- `is_public: bool` — optional (default `False`)
- `resource_id: str | None` — optional
- `is_locked: bool` — optional (default `False`)
- `sleep_mode: SleepMode` — optional (default `'skip'`)
- `created_by: str | None` — optional
- `created_by_name: str | None` — optional
- `created_at: datetime` — required
- `updated_at: datetime | None` — optional
- `use_rerank: bool | None` — optional
- `reranker_provider: str | None` — optional
- `embedding_model: str | None` — optional
- `embedding_dimensions: int | None` — optional
- `member_count: int | None` — optional
- `memory_count: int` — optional (default `0`)
- `last_activity_at: datetime | None` — optional

### ContextListResponse (BaseModel, L253)
> Response model for context list.
- `contexts: list[ContextResponse]` — required
- `total: int` — required

### ContextStatsResponse (BaseModel, L261)
> Response model for context statistics.
- `context_id: UUID` — required
- `context_name: str` — required
- `memory_count: int` — required
- `status: str` — required

### ContextTagsResponse (BaseModel, L273)
> Response model for ``GET /contexts/{context_id}/tags`` (Issue #614).
- `context_id: UUID` — required
- `tags: list[RelatedTagItem]` — required
- `total: int` — required

### ContextMemberResponse (BaseModel, L1107)
> Response model for context member.
- `user_id: str` — required
- `user_name: str | None` — optional
- `user_email: str | None` — optional
- `role: str` — required
- `added_at: str | None` — optional
- `is_workspace_admin: bool` — optional (default `False`)

### AddContextMemberRequest (BaseModel, L1131) (request model)
> Request model for adding context member.
- `user_id: str` — required
- `role: str` — required

### UpdateContextMemberRoleRequest (BaseModel, L1138) (request model)
> Request model for updating context member role.
- `role: str` — required

### MemoryStatItem (BaseModel, L1560)
> Per-memory usage statistics.
- `id: str` — required
- `summary: str` — required
- `type: str` — required
- `importance: float` — required
- `scope: str` — required
- `use_count: int` — required
- `access_count: int` — required
- `last_used_at: str | None` — required
- `embedding_status: str` — required
- `created_at: str` — required

### MemoryUsageStatsResponse (BaseModel, L1575)
> Response for per-memory stats endpoint.
- `memories: list[MemoryStatItem]` — required
- `total: int` — required
- `sort_by: str` — required
- `sort_order: str` — required

### DuplicateMemoryInfo (BaseModel, L1665)
> Memory info for duplicate pair display.
- `id: str` — required
- `summary: str` — required
- `type: str` — required
- `created_at: str` — required

### DuplicatePair (BaseModel, L1674)
> A pair of similar memories.
- `memory_a: DuplicateMemoryInfo` — required
- `memory_b: DuplicateMemoryInfo` — required
- `similarity: float` — required

### DuplicatesResponse (BaseModel, L1682)
> Response for duplicate detection endpoint.
- `pairs: list[DuplicatePair]` — required
- `total_pairs: int` — required
- `threshold: float` — required
- `memories_scanned: int` — required

## cost_aggregation.py

(Admin-only routes — `require_admin` guard.)

### CostBreakdownByModelResponse (TZAwareBaseModel, L59)
> Per-model cost split inside a CostAggregationRowResponse.
- `model: str | None` — required
- `calls: int` — required
- `cost_usd: float | None` — required
- `cost_usd_byok: float | None` — required

### CostBreakdownBySourceResponse (TZAwareBaseModel, L76)
> Per-source cost split inside a CostAggregationRowResponse.
- `source: str` — required
- `calls: int` — required
- `cost_usd: float | None` — required
- `cost_usd_byok: float | None` — required

### CostAggregationRowResponse (TZAwareBaseModel, L89)
> One (period × workspace × user) row in the aggregation response.
- `period_start: date` — required
- `workspace_id: UUID | None` — required
- `user_id: str` — required
- `calls: int` — required
- `tokens_in: int` — required
- `tokens_out: int` — required
- `tokens_cached_in: int` — required
- `tokens_cache_write: int` — required
- `embedding_tokens: int` — required
- `cost_usd: float | None` — required
- `cost_usd_byok: float | None` — required
- `cost_breakdown_by_model: list[CostBreakdownByModelResponse]` — required
- `cost_breakdown_by_source: list[CostBreakdownBySourceResponse]` — required

### CostAggregationResponse (TZAwareBaseModel, L118)
> Wrapper around the row list — keeps room for a future cursor.
- `rows: list[CostAggregationRowResponse]` — required

## external_keys.py

### ExternalKeyCreate (BaseModel, L46) (request model)
> Create external API key request.
- `key_name: str` — required
- `provider: str` — required
- `value: str` — required
- `enabled: bool` — optional (default `True`)

### ExternalKeyUpdate (BaseModel, L55) (request model)
> Update external API key request.
- `value: str` — required

### ExternalKeyToggle (BaseModel, L61) (request model)
> Toggle enabled/disabled state (Issue #105).
- `enabled: bool` — required

### ExternalKeyResponse (BaseModel, L67)
> External API key response (masked).
- `id: int` — required ⚠ sequential integer DB PK used as the public identifier (leaks row count; consider opaque id before freeze)
- `key_name: str` — required
- `provider: str` — required
- `masked_value: str` — required
- `user_id: str` — required
- `enabled: bool` — required
- `created_at: str` — required
- `updated_at: str` — required

### ExternalKeyListResponse (BaseModel, L80)
> External API keys list response.
- `keys: list[ExternalKeyResponse]` — required
- `total: int` — required

## feedback.py

### FeedbackRequest (BaseModel, L35) (request model)
> Body for recording retrieval feedback.
- `memory_id: UUID` — required
- `helpful: bool` — required
- `query: str | None` — optional
- `note: str | None` — optional

### FeedbackResponse (BaseModel, L52)
- `feedback_id: UUID` — required
- `memory_id: UUID` — required
- `helpful: bool` — required

## files.py

### FileReserveRequest (BaseModel, L48) (request model)
> Body for ``POST /api/v1/files/reserve``.
- `workspace_id: UUID` — required
- `filename: str` — required
- `content_type: str` — required
- `size_bytes: int` — required
- `sha256: str` — required

### FileReserveResponse (TZAwareBaseModel, L67)
- `file_id: UUID` — required
- `upload_url: str` — required
- `expires_at: datetime` — required

### FileConfirmRequest (BaseModel, L73) (request model)
> Body for ``POST /api/v1/files/{file_id}/confirm``.
- `sha256: str` — required

### FileObjectOut (TZAwareBaseModel, L83)
> Subset of ``FileObject`` exposed to clients.
- `id: UUID` — required
- `workspace_id: UUID` — required
- `filename: str` — required
- `content_type: str` — required
- `size_bytes: int` — required
- `sha256: str` — required
- `status: str` — required
- `created_at: datetime` — required
- `uploaded_at: datetime | None` — optional

### FileDownloadUrlOut (TZAwareBaseModel, L103)
- `download_url: str` — required

## graph.py

### GraphStats (BaseModel, L33)
> Graph statistics response.
- `total_nodes: int` — required
- `total_edges: int` — required
- `avg_edge_weight: float` — required
- `max_edge_weight: float` — required
- `min_edge_weight: float` — required
- `density: float` — required
- `top_connections: list[dict]` — required
- `recent_edges: list[dict]` — required

### GraphStatsResponse (BaseModel, L46)
> Graph stats API response.
- `user_id: str` — required
- `stats: GraphStats` — required
- `last_updated: str` — required

### GraphNode (BaseModel, L54)
> Graph node for visualization.
- `id: str` — required
- `summary: str` — required
- `type: str` — required
- `importance: float` — required
- `degree: int` — required
- `created_at: str | None` — optional

### GraphEdge (BaseModel, L65)
> Graph edge for visualization.
- `source: str` — required
- `target: str` — required
- `weight: float` — required
- `type: str` — required
- `created_at: str | None` — optional
- `confidence: float | None` — optional

### GraphDataResponse (BaseModel, L83)
> Graph data for visualization.
- `nodes: list[GraphNode]` — required
- `edges: list[GraphEdge]` — required
- `stats: dict` — required

## mcp.py

### MCPToolInfo (BaseModel, L23)
> MCP Tool information.
- `name: str` — required
- `description: str` — required
- `input_schema: dict` — required

### MCPToolsResponse (BaseModel, L31)
> MCP tools list response.
- `tools: list[MCPToolInfo]` — required
- `total: int` — required

### MCPStatusResponse (BaseModel, L38)
> MCP server status response.
- `status: str` — required
- `active_sessions: int` — required
- `total_tools: int` — required
- `tools_available: list[str]` — required
- `last_activity: str | None` — required

## me_account.py

### ErasureRequestCreateResponse (TZAwareBaseModel, L57)
> Returned after creating a self-service erasure request.
- `request_id: UUID` — required
- `status: str` — required
- `requested_at: datetime` — required
- `confirm_token: str | None` — optional ⚠ erasure confirmation token returned in the API body for password-auth users (by design per docstring; verify in-band token delivery is acceptable vs out-of-band before freeze)

### ErasureConfirmRequest (BaseModel, L101) (request model)
> Payload for POST /me/account/erasure-confirm.
- `token: str` — required
- `password: str | None` — optional

### ErasureRequestStateResponse (TZAwareBaseModel, L111)
> Read-only view of an erasure request's lifecycle state.
- `request_id: UUID` — required
- `status: str` — required
- `is_self_service: bool` — required
- `requested_at: datetime` — required
- `confirmed_at: datetime | None` — optional
- `scheduled_for: datetime | None` — optional
- `started_at: datetime | None` — optional
- `completed_at: datetime | None` — optional
- `cancelled_at: datetime | None` — optional
- `failure_reason: str | None` — optional

### LinkProviderRequest (BaseModel, L253) (request model)
> Body for POST /me/account/link-provider.
- `provider: Literal['google', 'github']` — required

### LinkProviderResponse (BaseModel, L259)
> Frontend redirects ``window.location`` to ``authorization_url``.
- `authorization_url: str` — required
- `state: str` — required

### UnlinkProviderRequest (BaseModel, L270) (request model)
> Body for POST /me/account/unlink-provider.
- `provider: Literal['google', 'github']` — required

### UnlinkProviderResponse (BaseModel, L276)
> Returned by POST /me/account/unlink-provider on success.
- `status: str` — required

### LinkedProvider (BaseModel, L282)
> One linked OAuth identity, as surfaced to the profile UI.
- `provider: str` — required
- `linked_at: str | None` — optional
- `last_used_at: str | None` — optional

### ProvidersListResponse (BaseModel, L295)
> All OAuth providers currently linked to the session user.
- `providers: list[LinkedProvider]` — required

## me_oauth.py

### RefreshOAuthRequest (BaseModel, L169) (request model)
> Optional ``return_to`` lets the frontend control the post-callback redirect.
- `return_to: str | None` — optional

### RefreshOAuthResponse (BaseModel, L180)
> Frontend redirects ``window.location`` to ``authorization_url``.
- `authorization_url: str` — required
- `state: str` — required

## memory.py

### MemoryListItem (BaseModel, L443)
> Memory list item.
- `id: str` — required
- `summary: str` — required
- `type: str` — required
- `scope: str` — required
- `importance: float` — required
- `created_at: str` — required
- `updated_at: str` — required

### MemoryListResponse (BaseModel, L455)
> Memory list response.
- `memories: list[MemoryListItem]` — required
- `total: int` — required
- `has_more: bool` — required

## neural_config.py

### NeuralConfigItem (TZAwareBaseModel, L33)
> Neural config item response.
- `key: str` — required
- `value: str` — required
- `value_type: str` — required
- `category: str` — required
- `description: str | None` — required
- `min_value: float | None` — required
- `max_value: float | None` — required
- `updated_at: datetime` — required

### NeuralConfigListResponse (BaseModel, L46)
> List of neural config items.
- `configs: list[NeuralConfigItem]` — required
- `categories: list[str]` — required
- `total: int` — required

### NeuralConfigUpdateRequest (BaseModel, L54) (request model)
> Update neural config request.
- `value: str` — required

### NeuralConfigUpdateResponse (BaseModel, L60)
> Update neural config response.
- `key: str` — required
- `old_value: str` — required
- `new_value: str` — required
- `message: str` — required

### NeuralConfigResetResponse (BaseModel, L69)
> Reset neural config response.
- `message: str` — required
- `reset_count: int` — required

## oauth.py

### OAuth2ClientResponse (BaseModel, L107)
> OAuth2 Client response (without secret).
- `id: int` — required
- `client_id: str` — required
- `client_name: str` — required
- `redirect_uris: list[str]` — required
- `grant_types: list[str]` — required
- `response_types: list[str]` — required
- `scope: str` — required
- `token_endpoint_auth_method: str` — required
- `owner_id: str | None` — required
- `provider: str` — required
- `created_at: str` — required
- `plaintext_secret: str | None` — optional ⚠ decrypted client secret in a response model whose docstring says "without secret" — gated by owner + visibility window in code, but verify gating and `response_model_exclude` coverage on every endpoint returning this model before freeze
- `is_visible: bool` — required
- `visibility_expires_at: str | None` — optional

### OAuth2ClientCreateRequest (BaseModel, L148) (request model)
> OAuth2 Client creation request.
- `client_name: str` — required
- `redirect_uris: list[str]` — required
- `provider: str` — optional (default `'custom'`)
- `grant_types: list[str]` — optional (default `['authorization_code', 'refresh_token']`)
- `response_types: list[str]` — optional (default `['code']`)
- `scope: str` — optional (default `DCR_DEFAULT_SCOPE`)
- `token_endpoint_auth_method: str` — optional (default `'client_secret_post'`)

### DynamicClientRegistrationRequest (BaseModel, L178) (request model)
> Dynamic Client Registration (DCR) request for MCP clients.
- `client_name: str` — optional (default `'MCP Client'`)
- `redirect_uris: list[str]` — required
- `grant_types: list[str]` — optional (default `['authorization_code', 'refresh_token']`)
- `response_types: list[str]` — optional (default `['code']`)
- `scope: str | None` — optional
- `token_endpoint_auth_method: str` — optional (default `'none'`)

### OAuth2ClientUpdateRequest (BaseModel, L204) (request model)
> OAuth2 Client update request.
- `client_name: str | None` — optional
- `redirect_uris: list[str] | None` — optional
- `scope: str | None` — optional
- `token_endpoint_auth_method: str | None` — optional

### OAuth2ProviderResponse (BaseModel, L223)
> OAuth2 Provider response.
- `name: str` — required
- `display_name: str` — required
- `client_id: str | None` — required
- `authorization_url: str` — required
- `token_url: str` — required
- `scopes: list[str]` — required
- `enabled: bool` — required
- `configured: bool` — required

### DeviceAuthorizationRequest (BaseModel, L241) (request model)
> Device Authorization Request (RFC 8628 Section 3.1).
- `client_id: str` — required
- `scope: str | None` — optional

### DeviceAuthorizationResponse (TZAwareBaseModel, L248)
> Device Authorization Response (RFC 8628 Section 3.2).
- `device_code: str` — required
- `user_code: str` — required
- `verification_uri: str` — required
- `verification_uri_complete: str` — required
- `expires_in: int` — required
- `interval: int` — required

### DeviceVerifyRequest (BaseModel, L259) (request model)
> Request to look up pending device authorization by user_code.
- `user_code: str` — required

### DeviceVerifyResponse (TZAwareBaseModel, L265)
> Pending device authorization info returned for the consent screen.
- `user_code: str` — required
- `client_name: str` — required
- `scope: str | None` — required
- `expires_at: str` — required
- `is_authorized: bool` — required
- `is_expired: bool` — required

### DeviceConfirmRequest (BaseModel, L276) (request model)
> User consent decision for a pending device authorization.
- `user_code: str` — required
- `approve: bool` — optional (default `True`)

### DeviceConfirmResponse (TZAwareBaseModel, L283)
> Result of user consent decision.
- `status: str` — required
- `user_code: str` — required

### DeviceUnauthAuditRequest (BaseModel, L290) (request model)
> Fire-and-forget audit payload for unauthenticated /device hits (Issue #779).
- `user_code_prefix: str` — optional (default `''`)

## public_search.py

### PublicSearchRequest (BaseModel, L39) (request model)
> Public search request.
- `query: str` — required
- `limit: int` — optional (default `10`)
- `use_rerank: bool` — optional (default `False`)
- `search_mode: str` — optional (default `'hybrid'`)

### PublicSearchResult (BaseModel, L52)
> Single search result with schema-aware formatting.
- `memory_id: str` — required
- `content: str` — required
- `score: float` — required
- `metadata: dict` — required ⚠ untyped dict on an anonymous-capable public endpoint — leak surface depends entirely on upstream classification filtering; unfreezable shape as-is
- `highlighted: dict | None` — optional

### PublicSearchResponse (BaseModel, L62)
> Public search response.
- `status: str` — optional (default `'success'`)
- `query: str` — required
- `results: list[PublicSearchResult]` — required
- `count: int` — required
- `context_id: str` — required
- `resource_id: str | None` — optional
- `schema_version: int | None` — optional

## resource_indexer.py

### IndexerStateMetrics (BaseModel, L50)
> Per-run indexer metrics, flattened from the JSONB column.
- `applied_upserts: int` — optional (default `0`)
- `applied_deletes: int` — optional (default `0`)
- `errors: int` — optional (default `0`)
- `skipped_reason: IndexerSkippedReason | None` — optional

### IndexerState (BaseModel, L65)
> Indexer state snapshot for one resource/context.
- `job_status: IndexerJobStatus` — required
- `last_run_at: str | None` — optional
- `next_run_at: str | None` — optional
- `active_version: int` — required
- `last_offset: int` — required
- `lag_seconds: float | None` — optional
- `metrics: IndexerStateMetrics` — required

### ResourceEventItem (BaseModel, L100)
> Single row in the recent ingest events list.
- `id: int` — required
- `op: Literal['upsert', 'delete']` — required
- `doc_id: str` — required
- `version: int | None` — optional
- `created_at: str | None` — optional

### IndexerStatusResponse (BaseModel, L116)
> Response body for ``GET /api/v1/resources/{resource_id}/indexer-status``.
- `resource_id: str` — required
- `state: IndexerState | None` — optional
- `recent_events: list[ResourceEventItem]` — optional (default `list()`)

## resource_schema.py

### FieldDefinition (BaseModel, L35)
> Field metadata definition.
- `name: str` — required
- `type: str` — required
- `description: str` — required
- `classification: str` — optional (default `'public'`)
- `index_hint: str` — optional (default `''`)
- `unit: str | None` — optional
- `enum_values: list[str] | None` — optional
- `example: str | None` — optional
- `required: bool` — optional (default `False`)

### SchemaCreateRequest (BaseModel, L63) (request model)
> Request to create a new resource schema.
- `resource_id: str` — required
- `field_definitions: list[FieldDefinition]` — required

### SchemaResponse (BaseModel, L72)
> Response with resource schema.
- `resource_id: str` — required
- `schema_version: int` — required
- `field_definitions: list[FieldDefinition]` — required
- `created_at: str` — required

### ResourceImpactResponse (BaseModel, L81)
> Response with resource change impact information.
- `resource_id: str` — required
- `token_count: int` — required
- `memory_count: int` — required
- `current_schema_version: int | None` — optional

## resource_tokens.py

### ResourceTokenCreate (BaseModel, L54) (request model)
> Request model for creating a resource token.
- `resource_id: str` — required
- `description: str | None` — optional
- `quota_events_per_hour: int` — optional (default `1000`)

### ResourceTokenUpdate (BaseModel, L66) (request model)
> Request model for updating a resource token.
- `description: str | None` — optional
- `quota_events_per_hour: int | None` — optional

### ResourceTokenResponse (TZAwareBaseModel, L75)
> Response model for resource token metadata (no plaintext).
- `id: int` — required ⚠ sequential integer DB PK used as the public identifier (leaks row count; consider opaque id before freeze)
- `resource_id: str` — required
- `description: str | None` — optional
- `quota_events_per_hour: int` — required
- `created_by: str | None` — optional
- `created_at: datetime` — required
- `last_used_at: datetime | None` — optional
- `is_active: bool` — required
- `status: Literal['active', 'revoked']` — required

### PaginatedResourceTokensResponse (BaseModel, L91)
> Paginated response for resource tokens.
- `tokens: list[ResourceTokenResponse]` — required
- `total: int` — required
- `limit: int` — required
- `offset: int` — required

## resources.py

### ResourceListItem (BaseModel, L44)
> Single resource entry in the workspace resource list.
- `resource_id: str` — required
- `context_id: str` — required
- `context_name: str` — required
- `context_display_name: str | None` — optional
- `token_count: int` — required
- `memory_count: int` — required
- `current_schema_version: int | None` — optional
- `created_at: str` — required
- `updated_at: str` — required

### ResourceListResponse (BaseModel, L66)
> Workspace resource list response.
- `resources: list[ResourceListItem]` — required
- `total: int` — required

### ResourceEventRecord (BaseModel, L260)
> A single ingest event row for the Resource Detail Data tab.
- `id: str` — required
- `op: str` — required
- `doc_id: str` — required
- `version: int | None` — optional
- `idempotency_key: str | None` — optional
- `importance: float` — required
- `created_at: str` — required
- `payload: dict | None` — optional ⚠ raw ingest payload returned verbatim (size-capped only) — confirm raw-vs-derived boundary / classification redaction (#968) applies before 1.0
- `event_metadata: dict | None` — optional ⚠ raw producer-supplied metadata, same raw-data-boundary concern as payload
- `payload_bytes: int` — required
- `payload_truncated: bool` — required

### ResourceEventsResponse (BaseModel, L297)
> Cursor-paginated resource events response.
- `events: list[ResourceEventRecord]` — required
- `next_cursor: str | None` — optional

## sleep_reports.py

### SleepReportSummary (TZAwareBaseModel, L46)
> Sleep report summary for list view.
- `id: UUID` — required
- `user_id: str` — required
- `workspace_id: UUID | None` — required
- `context_id: UUID | None` — required
- `context_name: str | None` — optional
- `status: str` — required
- `started_at: datetime` — required
- `completed_at: datetime | None` — required
- `memories_processed: int` — required
- `edges_created: int` — required
- `memories_merged: int` — required
- `memories_promoted: int` — required
- `memories_flagged: int` — required
- `llm_calls_made: int` — required
- `llm_tokens_used: int` — required

### SleepActionItem (TZAwareBaseModel, L79)
> Sleep action audit log entry.
- `id: int` — required
- `phase: str` — required
- `action_type: str` — required
- `memory_id: UUID | None` — required
- `target_id: UUID | None` — required
- `details: dict[str, Any] | None` — required
- `created_at: datetime` — required

### SleepReportListResponse (BaseModel, L91)
> List of sleep reports with pagination.
- `reports: list[SleepReportSummary]` — required
- `total: int` — required
- `limit: int` — required
- `offset: int` — required

### SleepReportDetailResponse (BaseModel, L100)
> Sleep report detail with all actions.
- `report: SleepReportDetail` — required (`SleepReportDetail(SleepReportSummary)` L66 adds `context_deleted`, `embedding_calls_made`, `error_message`, `edge_discovery_result`, `dedup_result`, `importance_result`, `consolidation_result`, `reindex_result` — five untyped `dict | None` pipeline internals)
- `actions: list[SleepActionItem]` — required
- `action_count: int` — required

## system.py

### ServiceStatus (BaseModel, L20)
> Individual service status.
- `status: str` — required
- `version: str | None` — optional
- `details: dict | None` — optional

### TelemetryResponse (BaseModel, L28)
> System telemetry response (endpoint is gated by APIKeyOrSessionUser, i.e. any authenticated user).
- `services: dict[str, ServiceStatus]` — required
- `embedding_config: dict | None` — optional ⚠ internal embedding infrastructure config exposed to any authenticated user — consider admin-only or remove before freeze
- `memory_stats: dict` — required
- `neural_memory: dict` — required
- `uptime_seconds: int` — required
- `version: str` — required

### EmbeddingModelInfo (BaseModel, L358)
> Individual embedding model info.
- `name: str` — required
- `dimensions: int` — required
- `provider: str` — required
- `available: bool` — required

### EmbeddingModelsResponse (BaseModel, L367)
> Response for available embedding models.
- `models: list[EmbeddingModelInfo]` — required
- `default_model: str` — required

## usage.py

### PlanLimits (BaseModel, L29)
> Plan limits and quotas.
- `plan_name: str` — required
- `memory_limit: int` — required
- `daily_total_limit: int` — required
- `weekly_total_limit: int` — required
- `mcp_calls_per_day: int` — optional (default `0`)
- `mcp_calls_per_week: int` — optional (default `0`)
- `rest_calls_per_day: int` — optional (default `0`)
- `rest_calls_per_week: int` — optional (default `0`)
- `public_calls_per_day: int` — optional (default `0`)
- `public_calls_per_week: int` — optional (default `0`)

### AnalysisUsage (BaseModel, L59)
> Memory broadlistening daily quota usage (Issue #496).
- `used_today: int` — optional (default `0`)
- `limit_today: int` — optional (default `0`)
- `addon_bonus: int` — optional (default `0`)
- `remaining_today: int` — optional (default `0`)
- `resets_at: str` — required

### SleepContextsUsage (BaseModel, L81)
> Sleep-enabled contexts quota usage (Issue #560).
- `used: int` — optional (default `0`)
- `limit: int` — optional (default `0`)
- `addon_bonus: int` — optional (default `0`)
- `remaining: int` — optional (default `0`)

### WorkspacesUsage (BaseModel, L102)
> Owned-workspace cap usage (Issue #661).
- `used: int` — optional (default `0`)
- `limit: int` — optional (default `0`)
- `remaining: int` — optional (default `0`)

### CurrentUsage (BaseModel, L126)
> Current usage statistics.
- `memory_count: int` — required
- `api_calls_today: int` — required
- `api_calls_this_week: int` — required
- `mcp_calls_today: int` — optional (default `0`)
- `mcp_calls_this_week: int` — optional (default `0`)
- `rest_calls_today: int` — optional (default `0`)
- `rest_calls_this_week: int` — optional (default `0`)
- `public_calls_today: int` — optional (default `0`)
- `public_calls_this_week: int` — optional (default `0`)
- `analysis: AnalysisUsage | None` — optional
- `sleep_contexts: SleepContextsUsage | None` — optional
- `workspaces: WorkspacesUsage` — required

### UsageStatus (BaseModel, L163)
> Usage status with percentage.
- `current: int | float` — required
- `limit: int | float` — required
- `percentage: float` — required
- `is_warning: bool` — required
- `is_critical: bool` — required
- `is_exceeded: bool` — required

### UsageCurrentResponse (BaseModel, L174)
> Current usage vs limits response.
- `plan: PlanLimits` — required
- `usage: CurrentUsage` — required
- `memory_usage: UsageStatus` — required
- `daily_api_usage: UsageStatus` — required
- `weekly_api_usage: UsageStatus` — required

### DailyUsage (BaseModel, L184)
> Daily usage statistics.
- `date: str` — required
- `count: int` — required

### UsageHistoryResponse (BaseModel, L191)
> Historical usage data.
- `daily_stats: list[DailyUsage]` — required
- `total_requests: int` — required
- `period_start: str` — required
- `period_end: str` — required

### EndpointUsage (BaseModel, L200)
> Usage by endpoint.
- `endpoint: str` — required
- `count: int` — required
- `percentage: float` — required

### UsageBreakdownResponse (BaseModel, L208)
> Usage breakdown by endpoint.
- `by_endpoint: list[EndpointUsage]` — required
- `total_requests: int` — required
- `period_days: int` — required

## workers.py

### WorkerConnectorConfig (TZAwareBaseModel, L60)
> Per-connector config handed to the ai-worker. Contains secrets. (Endpoint is gated by `verify_worker_token` — internal worker surface, not for the public 1.0 freeze.)
- `connector_id: UUID` — required
- `workspace_id: UUID` — required
- `context_id: UUID` — required
- `platform: str` — required
- `locale: str | None` — optional
- `slack: dict[str, Any]` — required ⚠ contains plaintext Slack connector secrets per docstring — keep this model out of the public OpenAPI/1.0 surface
- `kmc: dict[str, Any]` — required ⚠ contains plaintext KMC write key — same internal-surface concern
- `resource: dict[str, Any] | None` — optional
- `llm: dict[str, Any] | None` — optional
- `pii_guardrail_config: dict[str, Any] | None` — optional

## workspace.py

### PrivateContextAggregation (BaseModel, L50)
> Aggregated statistics for inaccessible private contexts.
- `context_count: int` — required
- `memory_count: int` — required

### ContextStats (BaseModel, L60)
> Statistics for a single context.
- `context_id: str` — required
- `context_name: str` — required
- `created_by: str | None` — required
- `created_by_name: str | None` — required
- `memory_count: int` — required
- `is_private: bool` — optional (default `False`)

### WorkspaceStatsResponse (BaseModel, L74)
> Aggregated statistics across all user's contexts.
- `total_memories: int` — required
- `context_count: int` — required
- `contexts: list[ContextStats]` — required
- `private_aggregation: PrivateContextAggregation | None` — optional
- `plan_name: str` — required

### MemberUsageEntry (BaseModel, L575)
> Usage stats for a single workspace member.
- `user_id: str` — required
- `name: str | None` — required
- `email: str | None` — required
- `memory_count: int` — required
- `api_calls_today: int` — required
- `api_calls_week: int` — required

### MemberUsageResponse (BaseModel, L586)
> Per-member usage breakdown for workspace.
- `members: list[MemberUsageEntry]` — required
- `total_members: int` — required

### FailedMemoryInfo (BaseModel, L681)
> Info about a failed embedding memory.
- `id: str` — required
- `summary: str` — required
- `embedding_error: str | None` — required
- `created_at: str` — required
- `updated_at: str | None` — required

### EmbeddingStatusResponse (BaseModel, L691)
> Embedding queue status response.
- `total: int` — required
- `by_status: dict[str, int]` — required
- `failed_memories: list[FailedMemoryInfo]` — required

## workspace_connectors.py

### WorkspaceConnectorCreateRequest (BaseModel, L26) (request model)
> Request body for provisioning an ai-worker connector.
- `connector_type: Literal['slack', 'discord', 'teams']` — required
- `resource_id: str` — required
- `display_name: str | None` — optional
- `oauth_tokens: dict[str, Any] | None` — optional
- `pii_guardrail_config: dict[str, Any] | None` — optional
- `litellm_virtual_key_id: str | None` — optional
- `virtual_key_valid_until: datetime | None` — optional
- `quota_events_per_hour: int` — optional (default `1000`)
- `context_id: UUID | None` — optional
- `auto_create_context_name: str | None` — optional
- `llm_config: dict[str, Any] | None` — optional
- `channel_ids: list[Any] | None` — optional
- `locale: str | None` — optional
- `external_team_id: str | None` — optional
- `slack_install_handle: str | None` — optional

### WorkspaceConnectorCreateResponse (BaseModel, L67)
> Connector setup response. The token + KMC key are shown exactly once.
- `connector_id: UUID` — required
- `connector_type: str` — required
- `resource_id: str` — required
- `resource_pk: UUID` — required ⚠ internal DB primary key leaked alongside the public `resource_id` — hide or rename before freeze
- `context_id: UUID | None` — optional
- `token_id: int` — required
- `token: str` — required ⚠ plaintext connector token (shown-once by design — verify never logged/cached downstream)
- `kmc_api_key: str | None` — optional ⚠ plaintext KMC write key (shown-once by design)
- `quota_events_per_hour: int` — required
- `idempotency_key_prefix: str` — required

### WorkspaceConnectorSummary (TZAwareBaseModel, L84)
> One connector row for the workspace list view.
- `connector_id: UUID` — required
- `connector_type: str` — required
- `resource_pk: UUID` — required ⚠ internal DB primary key in a routine list view — should expose the public `resource_id` instead
- `context_id: UUID | None` — optional
- `config_version: int` — required
- `created_at: datetime` — required
- `created_by: str | None` — optional

### RotateKmcKeyResponse (TZAwareBaseModel, L96)
> Response after rotating a connector's KMC write key. Shown exactly once.
- `connector_id: UUID` — required
- `kmc_api_key: str` — required
- `kmc_api_key_expires_at: datetime` — required
- `config_version: int` — required

## workspace_plan.py

### WorkspacePlanInfo (BaseModel, L45)
> Workspace plan information with usage stats. (Name collides with `admin_plans.WorkspacePlanInfo` — OpenAPI schema-name ambiguity.)
- `workspace_id: str` — required
- `workspace_name: str` — required
- `current_plan: str` — required
- `plan_display_name: str` — required
- `price_monthly: int` — required
- `usage: dict` — required
- `quotas: dict` — required
- `can_upgrade: bool` — required
- `can_downgrade: bool` — required

### AvailablePlanInfo (BaseModel, L59)
> Available plan tier information.
- `name: str` — required
- `display_name: str` — required
- `price_monthly: int` — required
- `quotas: dict` — required
- `features: list[str]` — required

### UpdatePlanRequest (BaseModel, L69) (request model)
> Request to update workspace plan. (Name collides with `admin_plans.UpdatePlanRequest`.)
- `plan_name: str` — required
- `reason: str | None` — optional

## workspaces.py

### WorkspaceCreate (BaseModel, L38) (request model)
> Request model for creating workspace.
- `name: str` — required
- `openai_api_key: str | None` — optional
- `description: str | None` — optional
- `default_context_name: str | None` — optional
- `default_context_summary: str | None` — optional
- `default_context_usage_guide: str | None` — optional
- `default_context_embedding_model: str | None` — optional

### WorkspaceUpdate (BaseModel, L58) (request model)
> Request model for updating workspace.
- `name: str | None` — optional
- `description: str | None` — optional

### WorkspaceResponse (BaseModel, L68)
> Response model for workspace.
- `id: UUID` — required
- `name: str` — required
- `description: str | None` — required
- `owner_user_id: str` — required
- `plan_name: str` — required
- `member_count: int` — required
- `context_count: int` — required
- `created_at: str` — required
- `current_user_role: str | None` — optional
- `analyses_enabled: bool` — optional (default `False`)

### CredentialsStatusInfo (BaseModel, L89)
> Credentials status information for member.
- `api_key_count: int` — optional (default `0`)
- `api_key_visible: bool` — optional (default `False`)
- `claude_app_visible: bool | None` — optional
- `chatgpt_app_visible: bool | None` — optional
- `custom_app_count: int` — optional (default `0`)

### ContextStatsItem (TZAwareBaseModel, L99)
> Context usage statistics item.
- `context_id: str` — required
- `context_name: str` — required
- `memory_count: int` — required
- `last_activity: datetime | None` — required
- `member_count: int` — required
- `api_calls_week: int` — optional (default `0`)
- `active_users_week: int` — optional (default `0`)
- `avg_response_time_ms: float` — optional (default `0.0`)

### WorkspaceTotals (BaseModel, L115)
> Workspace-wide totals.
- `memory_count: int` — required

### ContextStatsResponse (BaseModel, L124)
> Response model for context statistics. (Name collides with `contexts.ContextStatsResponse` — different shape, OpenAPI schema-name ambiguity.)
- `contexts: list[ContextStatsItem]` — required
- `total_contexts: int` — required
- `workspace_totals: WorkspaceTotals` — required

### DailyUsageItem (BaseModel, L135)
> Daily usage statistics item.
- `date: str` — required
- `api_calls: int` — required
- `unique_users: int` — required

### UserActivityItem (TZAwareBaseModel, L146)
> User activity statistics item.
- `user_id: str` — required
- `user_name: str | None` — required
- `user_email: str | None` — required
- `api_calls: int` — required
- `last_activity: datetime | None` — required

### ContextUsageTimelineResponse (BaseModel, L159)
> Response model for context usage timeline.
- `context_id: str` — required
- `context_name: str` — required
- `daily_usage: list[DailyUsageItem]` — required
- `total_calls: int` — required

### ContextUserActivityResponse (BaseModel, L171)
> Response model for context user activity.
- `context_id: str` — required
- `context_name: str` — required
- `users: list[UserActivityItem]` — required
- `total_users: int` — required

### DailyMemoryCount (BaseModel, L183)
> Daily memory count item for timeline.
- `date: str` — required
- `count: int` — required

### MemoryTimelineResponse (BaseModel, L193)
> Response model for workspace memory timeline.
- `workspace_id: str` — required
- `workspace_name: str` — required
- `daily_counts: list[DailyMemoryCount]` — required
- `memories_created_in_period: int` — required
- `period_start: str` — required
- `period_end: str` — required

### TimelineItem (BaseModel, L207)
> Timeline data point.
- `date: str` — required
- `count: int` — required

### SearchTimelineItem (BaseModel, L217)
> Search timeline data point with anonymous/authenticated split.
- `date: str` — required
- `total: int` — required
- `anonymous: int` — required
- `authenticated: int` — required

### ResourceIngestStats (BaseModel, L229)
> Resource Ingest API statistics.
- `total_events: int` — required
- `last_n_days: int` — required
- `avg_per_day: float` — required
- `active_tokens: int` — required
- `timeline: list[TimelineItem]` — required

### PublicSearchStats (BaseModel, L242)
> Public Search API statistics.
- `total_searches: int` — required
- `last_n_days: int` — required
- `anonymous: int` — required
- `authenticated: int` — required
- `timeline: list[SearchTimelineItem]` — required

### PublicAPIStatsResponse (BaseModel, L255)
> Response model for public API statistics.
- `resource_ingest: ResourceIngestStats` — required
- `public_search: PublicSearchStats` — required

### WorkspaceMemberResponse (BaseModel, L265)
> Response model for workspace member.
- `user_id: str` — required
- `user_name: str | None` — optional
- `user_email: str | None` — optional
- `role: str` — required
- `joined_at: str | None` — required
- `credentials_status: CredentialsStatusInfo | None` — optional
- `last_login_at: str | None` — optional
- `allowed_context_ids: list[str] | None` — optional

### AddMemberRequest (BaseModel, L286) (request model)
> Request model for adding member.
- `user_id: str` — required
- `role: str` — required

### UpdateMemberRoleRequest (BaseModel, L293) (request model)
> Request model for updating member role.
- `role: str` — required

### UpdateMemberContextAccessRequest (BaseModel, L299) (request model)
> Request model for updating member's context access.
- `allowed_context_ids: list[str] | None` — optional

## Follow-up candidates

Issue #622 allows at most 2 follow-up sub-issues; candidates below are grouped into two proposed bundles.

### Bundle A — Internal-data and secret exposure hygiene (proposed sub-issue 1)

| Candidate | Priority | Action |
|---|---|---|
| `WorkspaceConnectorCreateResponse.resource_pk`, `WorkspaceConnectorSummary.resource_pk` (workspace_connectors.py) | P1 | Hide internal DB PK; expose only the public `resource_id` |
| `TelemetryResponse.embedding_config` (system.py) | P1 | Make admin-only or drop — internal embedding infra config currently visible to any authenticated user |
| `ResourceEventRecord.payload` / `event_metadata` (resources.py) | P1 | Confirm raw-vs-derived boundary (#968) / classification redaction applies to the Resource Detail Data tab before freeze |
| `OAuth2ClientResponse.plaintext_secret` (oauth.py) | P1 | Verify owner+visibility gating and `response_model_exclude` coverage on every endpoint returning this model; fix the misleading "without secret" docstring |
| `WorkerConnectorConfig` (workers.py) | P1 | Explicitly exclude the `/workers` surface (plaintext Slack/KMC secrets) from the public 1.0 OpenAPI/freeze scope |
| `ErasureRequestCreateResponse.confirm_token` (me_account.py) | P2 | Decide whether in-band token delivery for password-auth users is acceptable at 1.0, or move out-of-band |
| `WorkspaceConnectorCreateResponse.token` / `kmc_api_key`, `RotateKmcKeyResponse.kmc_api_key` | P2 | Shown-once by design; audit that they are never logged/cached downstream and document the contract |

### Bundle B — Identifier and shape freeze hygiene (proposed sub-issue 2)

| Candidate | Priority | Action |
|---|---|---|
| `APIKeyResponse.id`, `ExternalKeyResponse.id`, `ResourceTokenResponse.id`, `WorkspaceConnectorCreateResponse.token_id` (all `int`) | P2 | Replace sequential integer DB PKs with opaque/prefixed public identifiers before freezing the surface |
| Untyped `dict` fields on freezable responses: `PublicSearchResult.metadata`, `GraphStats.top_connections`/`recent_edges`, `GraphDataResponse.stats`, `UserStats.*`, `WorkspacePlanInfo.usage`/`quotas` (workspace_plan.py), `AvailablePlanInfo.quotas`, `TelemetryResponse.memory_stats`/`neural_memory`, `SleepReportDetail.*_result`, `UserInfo.workspaces` | P2 | Type them with explicit models, or mark them explicitly non-frozen in the 1.0 contract |
| Duplicate class names across route files: `WorkspacePlanInfo` and `UpdatePlanRequest` (admin_plans.py vs workspace_plan.py), `ContextStatsResponse` (contexts.py vs workspaces.py) | P2 | Rename one of each pair to avoid OpenAPI schema-name ambiguity at freeze time |
