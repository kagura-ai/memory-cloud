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

// Exact mirror of backend `MemoryListItem` (routes/memory.py:324) — the row
// shape returned by the UUID-addressed `GET /api/v1/memory/list` endpoint.
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
// `POST /api/v1/memory/reference`.
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

// Generic over the row shape so callers can specialize when needed.
// Default is `MemoryListItem` (the structurally correct type for
// `GET /api/v1/memory/list`).
export interface MemoryListResponse<TItem = MemoryListItem> {
  memories: TItem[];
  total: number;
  has_more: boolean;
}
