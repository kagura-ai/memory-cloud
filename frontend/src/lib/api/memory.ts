/**
 * Memory API Client
 *
 * Functions for interacting with Kagura Memory Cloud API
 */

import { apiClient } from "./base";
import type {
  Memory,
  MemoryScope,
  CreateMemoryRequest,
  UpdateMemoryRequest,
  MemoryListResponse,
  MemoryListItem,
  MemoryReference,
  MemoryStatsResponse,
} from "../types/memory";

// Issue #431/#433: Backend `GET /api/v1/memory/list` accepts only
// {context_id, scope, type, limit, offset} — the old getMemories params
// (query, agent_name, tags, min_importance, max_importance) don't exist
// on this endpoint. Named params object, not the legacy `MemorySearchParams`.
export interface ListMemoriesParams {
  context_id?: string;
  scope?: MemoryScope;
  type?: string;
  limit?: number;
  offset?: number;
}

/**
 * List memories, optionally scoped to a single context.
 *
 * Hits `GET /api/v1/memory/list` and returns the canonical
 * `MemoryListItem` row shape (no legacy composite-key fields).
 */
export async function getMemories(
  params: ListMemoriesParams = {},
): Promise<MemoryListResponse<MemoryListItem>> {
  const searchParams = new URLSearchParams();

  if (params.context_id) searchParams.set("context_id", params.context_id);
  if (params.scope) searchParams.set("scope", params.scope);
  if (params.type) searchParams.set("type", params.type);
  searchParams.set("limit", String(params.limit ?? 50));
  searchParams.set("offset", String(params.offset ?? 0));

  return apiClient.get<MemoryListResponse<MemoryListItem>>(
    `/api/v1/memory/list?${searchParams.toString()}`,
  );
}

/**
 * Fetch full memory detail by UUID.
 *
 * Returns the backend's `ReferenceResponse` shape verbatim. Consumers that
 * need the legacy `Memory` shape must adapt — the backend does not return
 * `key` / `value` / `agent_name` / `user_id` / `updated_at` / `access_count`.
 */
export async function referenceMemory(
  memoryId: string,
): Promise<MemoryReference> {
  return apiClient.post<MemoryReference>("/api/v1/memory/reference", {
    memory_id: memoryId,
  });
}

/**
 * Delete a memory by UUID (UUID-addressed forget, not the composite-key
 * DELETE which doesn't exist as a REST endpoint).
 */
export async function forgetMemory(memoryId: string): Promise<void> {
  await apiClient.post<unknown>("/api/v1/memory/forget", {
    memory_id: memoryId,
  });
}

// =============================================================================
// LEGACY DEAD CODE — composite-key helpers below this banner.
//
// These functions target backend paths that DO NOT EXIST on the current API
// surface (the memory router only exposes `/api/v1/memory/list`,
// `/remember`, `/reference`, `/forget`, `/recall`, `/explore`, `/stats`,
// `/access-patterns`). Calling any of them today returns 404.
//
// They remain in the file because the dialog components still import them
// (CreateMemoryDialog → createMemory, EditMemoryDialog → updateMemory,
// MemoriesTable → bulkDeleteMemories). Those dialogs are not yet wired to
// any consumer. Removal is tracked alongside the dialog refactors:
//   - createMemory / CreateMemoryDialog → needs /remember rewire
//   - updateMemory / EditMemoryDialog   → see #439 (UUID PUT endpoint)
//   - deleteMemory                       → superseded by forgetMemory above
//   - bulkDeleteMemories                 → blocked on a UUID bulk-forget
//   - searchMemoriesSemantic / Keyword / Timeline / getMemory / getMemoryStats
//                                         → broken legacy search/stats
//
// Do NOT add new callers. The single-source canonical list/read/delete is
// `getMemories` / `referenceMemory` / `forgetMemory` above.
// =============================================================================

/**
 * @deprecated Backend path /memory/{key} does not exist. Use referenceMemory(id).
 */
export async function getMemory(
  key: string,
  scope: string = "persistent",
  agentName: string = "global",
): Promise<Memory> {
  const searchParams = new URLSearchParams({
    scope,
    agent_name: agentName,
  });

  return apiClient.get<Memory>(
    `/memory/${encodeURIComponent(key)}?${searchParams.toString()}`,
  );
}

/**
 * @deprecated Backend path POST /memory does not exist. Use POST /api/v1/memory/remember.
 */
export async function createMemory(
  userId: string,
  data: CreateMemoryRequest,
): Promise<Memory> {
  return apiClient.post<Memory>("/memory", {
    user_id: userId,
    ...data,
  });
}

/**
 * @deprecated Backend path PUT /memory/{key} does not exist. See #439.
 */
export async function updateMemory(
  key: string,
  userId: string,
  data: UpdateMemoryRequest,
  scope: string = "persistent",
  agentName: string = "global",
): Promise<Memory> {
  return apiClient.put<Memory>(`/memory/${encodeURIComponent(key)}`, {
    user_id: userId,
    scope,
    agent_name: agentName,
    ...data,
  });
}

/**
 * @deprecated Backend path DELETE /memory/{key} does not exist. Use forgetMemory(id).
 */
export async function deleteMemory(
  key: string,
  userId: string,
  scope: string = "persistent",
  agentName: string = "global",
): Promise<void> {
  const searchParams = new URLSearchParams({
    user_id: userId,
    scope,
    agent_name: agentName,
  });

  return apiClient.delete<void>(
    `/memory/${encodeURIComponent(key)}?${searchParams.toString()}`,
  );
}

/**
 * @deprecated Backend path is GET /api/v1/memory/stats and derives user from auth, not query.
 */
export async function getMemoryStats(
  userId: string,
): Promise<MemoryStatsResponse> {
  return apiClient.get<MemoryStatsResponse>(`/memory/stats?user_id=${userId}`);
}

/**
 * @deprecated Backend path POST /memory/bulk-delete does not exist. Needs UUID bulk-forget.
 */
export async function bulkDeleteMemories(
  keys: string[],
  scope: "working" | "persistent" = "persistent",
  agentName: string = "global",
): Promise<{
  deleted_count: number;
  failed_keys: string[];
  errors: Record<string, string>;
}> {
  return apiClient.post("/memory/bulk-delete", {
    keys,
    scope,
    agent_name: agentName,
  });
}

// ============================================================================
// Issue #720: New MCP Tools Integration - Search Functions
// ============================================================================

export interface SemanticSearchParams {
  query: string;
  k?: number;
  agent_name?: string;
}

export interface KeywordSearchParams {
  query: string;
  k?: number;
  agent_name?: string;
}

export interface TimelineSearchParams {
  time_range: string;
  event_type?: string;
  k?: number;
  agent_name?: string;
}

export async function searchMemoriesSemantic(
  params: SemanticSearchParams,
): Promise<MemoryListResponse<Memory>> {
  const response = await apiClient.post<MemoryListResponse<Memory>>(
    "/memory/search-semantic",
    {
      query: params.query,
      k: params.k ?? 20,
      agent_name: params.agent_name ?? "global",
    },
  );
  return response;
}

export async function searchMemoriesKeyword(
  params: KeywordSearchParams,
): Promise<MemoryListResponse<Memory>> {
  const response = await apiClient.post<MemoryListResponse<Memory>>(
    "/memory/search-keyword",
    {
      query: params.query,
      k: params.k ?? 20,
      agent_name: params.agent_name ?? "global",
    },
  );
  return response;
}

export async function searchMemoriesTimeline(
  params: TimelineSearchParams,
): Promise<MemoryListResponse<Memory>> {
  const response = await apiClient.post<MemoryListResponse<Memory>>(
    "/memory/search-timeline",
    {
      time_range: params.time_range,
      event_type: params.event_type,
      k: params.k ?? 20,
      agent_name: params.agent_name ?? "global",
    },
  );
  return response;
}
