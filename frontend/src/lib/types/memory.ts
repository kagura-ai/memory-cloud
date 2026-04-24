/**
 * Memory API Type Definitions
 *
 * TypeScript interfaces for Kagura Memory Cloud API
 */

export type MemoryScope = "working" | "persistent";
export type MemoryType = "normal" | "coding";

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
