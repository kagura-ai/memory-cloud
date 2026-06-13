/**
 * Memory API Client
 *
 * Functions for interacting with Kagura Memory Cloud API
 */

import { apiClient } from "./base";
import type {
  MemoryScope,
  MemoryListResponse,
  MemoryListItem,
  MemoryReference,
  RememberMemoryRequest,
  RememberMemoryResponse,
  RecallParams,
  RecallResponse,
} from "../types/memory";

// Issue #431/#433/#580: Backend `GET /api/v1/memory/list` accepts only
// {context_id, scope, type, q, limit, offset}.
//
// `q` (Issue #580): case-insensitive substring filter on memory summary.
// Trimmed client-side; whitespace-only values are dropped (the param is not
// sent at all) so the URL stays clean and the backend treats absent and
// whitespace-only identically.
export interface ListMemoriesParams {
  context_id?: string;
  scope?: MemoryScope;
  type?: string;
  q?: string;
  /** ANY-match tag filter (#618). Repeated as ?tags=a&tags=b; blanks ignored. */
  tags?: string[];
  /**
   * #830: how to combine `tags`. `"any"` (default — overlap, preserves #618)
   * or `"all"` (memory must hold every given tag). Only sent when `"all"`.
   */
  tagsMatch?: "any" | "all";
  limit?: number;
  offset?: number;
}

/**
 * List memories, optionally scoped to a single context.
 *
 * Hits `GET /api/v1/memory/list` and returns the canonical
 * `MemoryListItem` row shape.
 */
export async function getMemories(
  params: ListMemoriesParams = {},
): Promise<MemoryListResponse<MemoryListItem>> {
  const searchParams = new URLSearchParams();

  if (params.context_id) searchParams.set("context_id", params.context_id);
  if (params.scope) searchParams.set("scope", params.scope);
  if (params.type) searchParams.set("type", params.type);
  if (params.q) {
    const trimmed = params.q.trim();
    if (trimmed) searchParams.set("q", trimmed);
  }
  for (const tag of params.tags ?? []) {
    const trimmed = tag.trim();
    if (trimmed) searchParams.append("tags", trimmed);
  }
  // Only send tags_match when "all" — default "any" keeps the URL clean and
  // matches the backend default (#618 behavior preserved).
  if (params.tagsMatch === "all") searchParams.set("tags_match", "all");
  searchParams.set("limit", String(params.limit ?? 50));
  searchParams.set("offset", String(params.offset ?? 0));

  return apiClient.get<MemoryListResponse<MemoryListItem>>(
    `/api/v1/memory/list?${searchParams.toString()}`,
  );
}

/**
 * Save a new memory (Issue #952).
 *
 * Hits `POST /api/v1/memory/remember`. This is the only in-app memory writer —
 * the first-run onboarding flow uses it to save a sample memory so the user can
 * recall it and feel the value moment. Ongoing memory creation happens via MCP
 * tools, not the web UI. The request is forwarded verbatim; the backend fills
 * defaults for every field this client mirror omits.
 */
export async function rememberMemory(
  request: RememberMemoryRequest,
): Promise<RememberMemoryResponse> {
  return apiClient.post<RememberMemoryResponse>(
    "/api/v1/memory/remember",
    request,
  );
}

/**
 * Search memories with hybrid recall (Issue #952).
 *
 * Hits `POST /api/v1/memory/recall`. Honors the design constraint that recall
 * does NOT auto-trigger explore — this is precision search only. Pass
 * `filters.context_id` to scope to a single context.
 */
export async function recallMemories(
  params: RecallParams,
): Promise<RecallResponse> {
  return apiClient.post<RecallResponse>("/api/v1/memory/recall", params);
}

/**
 * Fetch full memory detail by UUID.
 *
 * Returns the backend's `ReferenceResponse` shape verbatim.
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

/**
 * Partial update of a memory by UUID (Issue #439).
 *
 * Backend route is `PATCH /api/v1/memory/{memory_id}` and accepts any
 * subset of the patchable fields. Omitted fields preserve their current
 * value; `tags` follows replace-all semantics (an empty array clears tags).
 *
 * Status code surfacing:
 *   - 200: returns the full `MemoryReference`
 *   - 404: memory does not exist OR caller lacks access (existence not leaked)
 *   - 410: memory was soft-deleted (distinct from 404 so retries can stop)
 *   - 422: validation error (empty patch, importance > 1, etc.)
 *
 * Caller propagates errors as `ApiError` (see `lib/api/base.ts`).
 */
export interface UpdateMemoryByIdPatch {
  summary?: string;
  content?: string;
  type?: string;
  importance?: number;
  tags?: string[];
  details?: Record<string, unknown> | null;
}

export async function updateMemoryById(
  memoryId: string,
  patch: UpdateMemoryByIdPatch,
): Promise<MemoryReference> {
  return apiClient.patch<MemoryReference>(
    `/api/v1/memory/${encodeURIComponent(memoryId)}`,
    patch,
  );
}
