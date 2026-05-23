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
  reranker_provider: "voyage" | "cohere" | "ollama";
  reranker_model: string;
  embedding_model?: string;
  embedding_dimensions?: number;
  created_at: string;
  updated_at: string;
}

export interface ContextSearchConfigUpdate {
  semantic_weight: number;
  bm25_weight: number;
  fetch_factor: number;
  use_rerank: boolean;
  reranker_provider: "voyage" | "cohere" | "ollama";
  reranker_model: string;
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
  // Cross-axis union: workspace owner/admin appear here via automatic access
  // (carrying WorkspaceRole), while explicit ContextMember rows carry ContextRole.
  role: WorkspaceRole | ContextRole;
  added_at: string | null; // null for workspace owners/admins with automatic access
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
