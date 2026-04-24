/**
 * Memory API Type Definitions
 *
 * TypeScript interfaces for Kagura Memory Cloud API
 */

export type MemoryScope = "working" | "persistent";

// Backend `Memory.type` is `Column(String(50))` (arbitrary string), so the
// wire type is `string`. `KnownMemoryType` enumerates values the UI recognizes
// for styling / badges — use it in lookups, not in type annotations.
export type MemoryType = string;
export type KnownMemoryType = "normal" | "coding";

// Issue #431/#432: Backend `MemoryListItem` (routes/memory.py:324) is UUID-keyed
// and returns {id, summary, type, scope, importance, created_at, updated_at}.
// Existing (legacy) endpoints still use composite (key, scope, agent_name)
// addressing with {key, value, agent_name, user_id, ...} shape.
//
// `id` is the new canonical row identity going forward. The legacy composite
// fields (key/value/agent_name/user_id) remain REQUIRED here even though the
// new `/memory/list` response does not populate them — making them optional
// would force null-guards into dialogs that are still dead code today
// (MemoryDetailDialog, EditMemoryDialog, DeleteMemoryDialog). That trade-off
// is intentional and type-unsound in one direction: a response from the new
// endpoint does not literally satisfy `Memory` without an `as` cast. The
// consumer issue (#433 Memories tab) will decide whether to weaken those
// fields to optional or to keep them required behind a conversion boundary.
export interface Memory {
  id: string;
  summary?: string;
  key: string;
  value: string;
  scope: MemoryScope;
  type?: MemoryType;
  agent_name: string;
  user_id: string;
  importance: number;
  metadata?: Record<string, unknown>;
  tags?: string[];
  created_at: string;
  updated_at: string;
  access_count?: number;
  last_accessed?: string;
}

export interface CreateMemoryRequest {
  key: string;
  value: string;
  scope?: MemoryScope;
  type?: MemoryType;
  agent_name?: string;
  importance?: number;
  metadata?: Record<string, unknown>;
  tags?: string[];
}

export interface UpdateMemoryRequest {
  value?: string;
  type?: MemoryType;
  importance?: number;
  metadata?: Record<string, unknown>;
  tags?: string[];
}

export interface MemorySearchParams {
  query?: string;
  scope?: MemoryScope;
  agent_name?: string;
  tags?: string[];
  min_importance?: number;
  max_importance?: number;
  limit?: number;
  offset?: number;
}

// Exact mirror of backend `MemoryListItem` (routes/memory.py:324) — the row
// shape returned by the new UUID-addressed `GET /memory/list` endpoint. Does
// NOT include legacy composite-key fields (key/value/agent_name/user_id).
// `type` is `string` (not `KnownMemoryType`) to match the backend column.
export interface MemoryListItem {
  id: string;
  summary: string;
  type: string;
  scope: MemoryScope;
  importance: number;
  created_at: string;
  updated_at: string;
}

// Response type is still typed as `Memory[]` (the superset) for backward
// compatibility with the pre-#432 getMemories() signature — see the `Memory`
// interface comment for the unsoundness this carries. The #433 consumer is
// expected to switch `memories` to `MemoryListItem[]` and let callers convert
// at the boundary when they need the full `Memory` shape.
export interface MemoryListResponse {
  memories: Memory[];
  total: number;
  has_more: boolean;
}

export interface MemoryStatsResponse {
  total_memories: number;
  working_memories: number;
  persistent_memories: number;
  total_size_bytes: number;
  avg_importance: number;
  agents: string[];
  tags: string[];
}

// ============================================================================
// Issue #720: Search result types
// ============================================================================

export interface SearchResultMemory extends Memory {
  score: number; // Relevance score (RAG similarity or BM25 score)
  search_mode?: "semantic" | "keyword" | "timeline";
}

export type SearchMode = "simple" | "semantic" | "keyword" | "timeline";
