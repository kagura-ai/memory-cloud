/**
 * Contexts API Client
 *
 * Issue #82 → #160: Context-based Multi-Collection Support (renamed from Projects)
 */

import { apiClient } from "./base";
import { ContextRole, WorkspaceRole } from "@/lib/auth/rbac";
import type {
  Context,
  ContextListResponse,
  ContextStats,
  CreateContextRequest,
  UpdateContextRequest,
} from "@/lib/types/context";

// Re-export types for consumers
export type { Context, ContextListResponse, ContextStats };

// ---------------------------------------------------------------------------
// Tags (#614 / #618): GET /contexts/{id}/tags drives the tag cloud + autocomplete
// ---------------------------------------------------------------------------

export type TagSortMode = "count" | "recent" | "alpha";

export interface ContextTagItem {
  tag: string;
  count: number;
  /** ISO-8601 UTC; present on list_tags (#614). */
  last_used_at?: string | null;
  sample_summary?: string | null;
}

export interface ContextTagsResponse {
  context_id: string;
  /** Context name (#1669); optional so older servers still type-check. */
  context_name?: string | null;
  tags: ContextTagItem[];
  total: number;
}

export interface GetContextTagsParams {
  prefix?: string;
  limit?: number;
  minCount?: number;
  sort?: TagSortMode;
  /** #618: facet the tags to memories whose summary matches this substring. */
  q?: string;
  /**
   * #830: multi-tag AND drill-down. Facets the cloud to tags that co-occur on
   * memories carrying ALL of these tags; the with_tags values are excluded
   * from the result. Sent as repeated `?with_tags=a&with_tags=b`.
   */
  withTags?: string[];
}

/**
 * List a context's tags with counts (#614). Powers the TagCloud (sort=count)
 * and TagAutocomplete (prefix + sort=count) on the context detail page (#618).
 */
export async function getContextTags(
  contextId: string,
  params: GetContextTagsParams = {},
): Promise<ContextTagsResponse> {
  const sp = new URLSearchParams();
  if (params.prefix) {
    const trimmed = params.prefix.trim();
    if (trimmed) sp.set("prefix", trimmed);
  }
  if (params.limit != null) sp.set("limit", String(params.limit));
  if (params.minCount != null) sp.set("min_count", String(params.minCount));
  if (params.sort) sp.set("sort", params.sort);
  if (params.q) {
    const trimmed = params.q.trim();
    if (trimmed) sp.set("q", trimmed);
  }
  for (const tag of params.withTags ?? []) {
    const trimmed = tag.trim();
    if (trimmed) sp.append("with_tags", trimmed);
  }
  const qs = sp.toString();
  return apiClient.get<ContextTagsResponse>(
    `/api/v1/contexts/${contextId}/tags${qs ? `?${qs}` : ""}`,
  );
}

/**
 * Get all contexts for the current user
 */
export async function getContexts(): Promise<ContextListResponse> {
  return apiClient.get<ContextListResponse>("/api/v1/contexts");
}

// Issue #246: getCurrentContext() removed (context always explicit from URL)

/**
 * Get a specific context by ID
 */
export async function getContext(contextId: string): Promise<Context> {
  return apiClient.get<Context>(`/api/v1/contexts/${contextId}`);
}

/**
 * Get context statistics
 */
export async function getContextStats(
  contextId: string,
): Promise<ContextStats> {
  return apiClient.get<ContextStats>(`/api/v1/contexts/${contextId}/stats`);
}

/**
 * Create a new context
 */
export async function createContext(
  data: CreateContextRequest,
): Promise<Context> {
  return apiClient.post<Context>("/api/v1/contexts", data);
}

/**
 * Update a context
 */
export async function updateContext(
  contextId: string,
  data: UpdateContextRequest,
): Promise<Context> {
  return apiClient.put<Context>(`/api/v1/contexts/${contextId}`, data);
}

// Issue #246: switchContext() removed (use URL navigation: router.push(`/memories?context=${contextId}`))

/**
 * Export a context as portable JSON (#950): its metadata, search config and
 * every memory the caller can see. Stays available while the workspace is
 * over its Free capacity (#1941), so the owner can keep a copy before deleting.
 */
export async function exportContext(contextId: string): Promise<unknown> {
  return apiClient.get<unknown>(`/api/v1/contexts/${contextId}/export`);
}

/**
 * Delete a context
 */
export async function deleteContext(contextId: string): Promise<void> {
  await apiClient.delete<void>(`/api/v1/contexts/${contextId}`);
}

// ============================================================================
// Context Search Config APIs (Issue #130)
// ============================================================================

export interface ContextSearchConfig {
  context_id: string;
  semantic_weight: number;
  bm25_weight: number;
  fetch_factor: number;
  use_rerank: boolean;
  reranker_provider: "voyage" | "cohere" | "self_hosted";
  reranker_model: string;
  embedding_model?: string;
  embedding_dimensions?: number;
  // #2015 / #2091: content chunk vectors. Optional throughout — a backend
  // predating them omits every field, and the settings card is then hidden.
  // The card also needs `content_chunks_can_change`, which a backend between
  // #2042 and #2091 does not send (#2119).
  // `content_chunks_enabled` is the STORED flag: never write
  // `content_chunks_effective` back into it (that would turn the flag off and
  // remove the chunk vectors).
  content_chunks_enabled?: boolean;
  /** The caller may change the flag: always to turn it off (owner rule
   * permitting); to turn it on only when the store, plan and cap allow. */
  content_chunks_can_change?: boolean;
  /** False on the LanceDB vector store, which keeps no chunk vectors. */
  content_chunks_supported?: boolean;
  /** Where memory content goes when chunks are on; null when unknown. */
  embedding_provider?: string | null;
  /** Memories still queued for chunk re-embedding; 0 while the flag is off. */
  content_chunks_backfill_pending?: number;
  // #2103 / #2080: whether the stored flag is acted on, and why not.
  content_chunks_effective?: boolean;
  /** The plan's cap on contexts with chunks; null where the deployment does
   * not gate content chunks by plan. */
  content_chunks_limit?: number | null;
  /** Workspace contexts with the flag stored on (this one included). */
  content_chunks_in_use?: number;
  content_chunks_disabled_reason?: ContentChunksDisabledReason | null;
  // #2144: the recall LLM selector. Optional throughout — a backend predating
  // it omits every field, and the settings card is then hidden.
  // `llm_selector_enabled` is the STORED flag: never write
  // `llm_selector_effective` back into it.
  llm_selector_enabled?: boolean;
  /** Whether recall acts on the stored flag (plan and cap, where gated). */
  llm_selector_effective?: boolean;
  /** The plan's cap on contexts with the selector; null where the deployment
   * does not gate the selector by plan. */
  llm_selector_limit?: number | null;
  /** Workspace contexts with the flag stored on (this one included). */
  llm_selector_in_use?: number;
  llm_selector_disabled_reason?: LlmSelectorDisabledReason | null;
  /** The caller may change the flag: the context owner or a workspace
   * owner/admin, and to turn it on, the plan's feature and a free slot. */
  llm_selector_can_change?: boolean;
  created_at: string;
  updated_at: string;
}

/** Why the content chunk toggle cannot be turned on, or the chunks are inactive. */
export type ContentChunksDisabledReason =
  "permission" | "unsupported" | "plan" | "cap";

/** Why the LLM selector toggle cannot be turned on, or the selector is inactive. */
export type LlmSelectorDisabledReason = "permission" | "plan" | "cap";

export interface ContextSearchConfigUpdate {
  semantic_weight: number;
  bm25_weight: number;
  fetch_factor: number;
  use_rerank: boolean;
  reranker_provider: "voyage" | "cohere" | "self_hosted";
  reranker_model: string;
  /** #2015: sent only when the user changed it (optional on the server). */
  content_chunks_enabled?: boolean;
  /** #2144: sent only when the user changed it (optional on the server). */
  llm_selector_enabled?: boolean;
}

/**
 * Get context search configuration
 */
export async function getContextSearchConfig(
  contextId: string,
): Promise<ContextSearchConfig> {
  return apiClient.get<ContextSearchConfig>(
    `/api/v1/contexts/${contextId}/search-config`,
  );
}

/**
 * Update context search configuration
 */
export async function updateContextSearchConfig(
  contextId: string,
  data: ContextSearchConfigUpdate,
): Promise<ContextSearchConfig> {
  return apiClient.put<ContextSearchConfig>(
    `/api/v1/contexts/${contextId}/search-config`,
    data,
  );
}

/**
 * Reset context search configuration to defaults
 */
export async function resetContextSearchConfig(
  contextId: string,
): Promise<ContextSearchConfig> {
  return apiClient.post<ContextSearchConfig>(
    `/api/v1/contexts/${contextId}/search-config/reset`,
  );
}

// ============================================================================
// Context Members Management (Issue #165)
// ============================================================================

export interface ContextMember {
  user_id: string;
  user_name: string | null;
  user_email: string | null;
  // Cross-axis union: workspace-role-derived rows carry WorkspaceRole (owners/admins and viewers),
  // while explicit ContextMember rows carry ContextRole.
  role: WorkspaceRole | ContextRole;
  added_at: string | null; // null only for workspace owner/admin rows (viewer rows use joined_at)
  is_workspace_admin: boolean; // true if access is via workspace role
}

export interface AddContextMemberRequest {
  user_id: string;
  role: ContextRole;
}

export interface UpdateContextMemberRoleRequest {
  role: ContextRole;
}

/**
 * List all members of a context
 */
export async function listContextMembers(
  contextId: string,
): Promise<ContextMember[]> {
  return apiClient.get<ContextMember[]>(
    `/api/v1/contexts/${contextId}/members`,
  );
}

/**
 * Add a member to a context
 */
export async function addContextMember(
  contextId: string,
  data: AddContextMemberRequest,
): Promise<ContextMember> {
  return apiClient.post<ContextMember>(
    `/api/v1/contexts/${contextId}/members`,
    data,
  );
}

/**
 * Update a context member's role
 */
export async function updateContextMemberRole(
  contextId: string,
  userId: string,
  data: UpdateContextMemberRoleRequest,
): Promise<ContextMember> {
  return apiClient.put<ContextMember>(
    `/api/v1/contexts/${contextId}/members/${userId}`,
    data,
  );
}

/**
 * Remove a member from a context
 */
export async function removeContextMember(
  contextId: string,
  userId: string,
): Promise<void> {
  return apiClient.delete<void>(
    `/api/v1/contexts/${contextId}/members/${userId}`,
  );
}

// ============================================================================
// Embedding Models (Issue #49)
// ============================================================================

export interface EmbeddingModel {
  name: string;
  dimensions: number;
  provider: string;
  available: boolean;
}

export interface EmbeddingModelsResponse {
  models: EmbeddingModel[];
  default_model: string;
}

/**
 * Get available embedding models with availability status
 */
export async function getEmbeddingModels(): Promise<EmbeddingModelsResponse> {
  return apiClient.get<EmbeddingModelsResponse>(
    "/api/v1/system/embedding/models",
  );
}
