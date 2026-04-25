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
// new `/api/v1/memory/list` response does not populate them — making them optional
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
  // Origin metadata (Issue #215). Optional — only populated for memories
  // imported from a vault, file, URL, or other tracked source.
  source_uri?: string | null;
  source_type?: string | null;
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
// shape returned by the new UUID-addressed `GET /api/v1/memory/list` endpoint. Does
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

// Issue #440: a single declared_link reference surfaced in MemoryReference.
// Mirror of backend `LinkedMemoryRef` (schemas.py:LinkedMemoryRef). Used by
// the dialog's "References" section — outgoing/incoming lists.
export interface LinkedMemoryRef {
  memory_id: string;
  summary: string;
  type?: string | null;
  importance: number;
  weight: number;
  created_at: string;
}

// Exact mirror of backend `ReferenceResponse` (schemas.py:222) — returned by
// `POST /api/v1/memory/reference`. This is NOT structurally equal to `Memory`:
// `key`, `value`, `agent_name`, `user_id`, and `access_count` are absent.
// (Issue #434 added `scope` and `updated_at` to the response so the dialog
// can render correctly from a deep-link without round-tripping through
// `/memory/list` to discover them.) Callers that feed a `MemoryDetailDialog`
// (which reads the absent legacy fields) must adapt at the boundary — see
// ``referenceAsMemory`` in ``components/contexts/MemoriesTabPanel.tsx`` for
// the canonical adapter.
//
// Issue #440: `outgoing_links`/`incoming_links` carry declared_link backlinks.
// Naming: `*_has_more` matches `MemoryListResponse.has_more` (codebase
// precedent for capped collections).
export interface MemoryReference {
  memory_id: string;
  summary: string;
  context_summary: string | null;
  content: string;
  details: Record<string, unknown> | null;
  type: string;
  scope: MemoryScope;
  importance: number;
  tags: string[];
  context: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  client: string;
  source_uri?: string | null;
  source_type?: string | null;
  outgoing_links?: LinkedMemoryRef[];
  outgoing_has_more?: boolean;
  incoming_links?: LinkedMemoryRef[];
  incoming_has_more?: boolean;
}

// Generic over the row shape: default is `MemoryListItem` (the structurally
// correct type for `GET /api/v1/memory/list`). Callers that still want the legacy
// superset shape (e.g., dead-code paths pre-dating the split) can specialize
// as `MemoryListResponse<Memory>`. Once the consumer (#433) lands, those
// legacy paths should convert at the boundary rather than carrying the
// superset through.
export interface MemoryListResponse<TItem = MemoryListItem> {
  memories: TItem[];
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
