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

// WHERE-axis (#1334) coordinates of a list row. Mirror of backend
// `MemoryListItemLocation` — present only when both generated columns
// (location_lat/location_lon) are populated.
export interface MemoryListItemLocation {
  lat: number;
  lon: number;
}

// Exact mirror of backend `MemoryListItem` (routes/memory.py) — the row
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
  // #2330: when the fact held — a date (YYYY-MM-DD) or a UTC datetime.
  observed_at?: string | null;
  // Always present on the wire (FastAPI serializes null without
  // response_model_exclude_none) — non-optional so the type matches the
  // actual contract.
  location: MemoryListItemLocation | null;
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

// #1403 mirror of backend `SupersedeCandidate` (schemas.py) — a near-duplicate
// this memory likely supersedes, detected at ingest. A *suggestion* only: the
// `supersedes` edge is created on explicit confirm via `POST /graph/edges`
// (#1416), never automatically. `similarity` is cosine in [0, 1]; `detected_at`
// is an ISO-8601 string (or null on legacy rows).
export interface SupersedeCandidate {
  memory_id: string;
  summary: string;
  similarity: number;
  detected_at: string | null;
  // #2016: true when the deployment's judge confirmed the update; absent / null otherwise.
  verified?: boolean | null;
  // #2427: whether the caller may accept the suggestion, computed by the
  // server on reference and the PATCH reply. Absent on recall rows and on an
  // older backend — the UI then keeps the action enabled and the server
  // decides on click.
  acceptance?: SupersedeAcceptance | null;
}

// #2427 mirror of backend `SupersedeAcceptance`. `reason` is set only when
// `allowed` is false; `not_owner` never says which memory is someone else's.
// #2518: `not_writable` includes an agent binding without write access, and
// `edge_exists` is the 409 of the click (another declared edge on the pair),
// reported before it.
export type SupersedeAcceptanceReason =
  | "not_writable"
  | "not_owner"
  | "newer_candidate"
  | "edge_exists";

export interface SupersedeAcceptance {
  allowed: boolean;
  reason?: SupersedeAcceptanceReason | null;
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
  // #2330: when the fact held — a date (YYYY-MM-DD) or a UTC datetime.
  observed_at?: string | null;
  client: string;
  source_uri?: string | null;
  source_type?: string | null;
  outgoing_links?: LinkedMemoryRef[];
  outgoing_has_more?: boolean;
  incoming_links?: LinkedMemoryRef[];
  incoming_has_more?: boolean;
  // #1403: liveness-guarded supersede suggestion. Present only when this memory
  // has a still-actionable near-duplicate it likely supersedes.
  supersede_candidate?: SupersedeCandidate | null;
}

// Issue #952: minimal client mirror of backend `RememberRequest`
// (schemas.py:59). The first-run onboarding flow is the only in-app writer of
// memories (everything else writes via MCP tools), so this carries just the
// fields onboarding needs — the backend defaults the rest.
export interface RememberMemoryRequest {
  /** Layer 1 search summary. Backend requires 10–500 chars. */
  summary: string;
  /** Layer 3 full content. Backend requires ≥1 char. */
  content: string;
  /** Memory type (arbitrary string, 1–50 chars). */
  type: string;
  context_summary?: string;
  importance?: number;
  tags?: string[];
  /** `{ context_id }` routes the memory into the right context collection. */
  context?: { context_id: string };
  details?: Record<string, unknown>;
}

// Mirror of backend `RememberResponse` (schemas.py:147).
export interface RememberMemoryResponse {
  status: string;
  memory_id: string;
  scope: string;
}

// Mirror of backend `RecallRequest` (schemas.py:155) — onboarding only needs
// query + a context filter, but the optional knobs are typed for reuse.
export interface RecallParams {
  query: string;
  k?: number;
  use_rerank?: boolean;
  search_mode?: "hybrid" | "semantic" | "keyword";
  /**
   * #2132: true returns memories shadowed by a `supersedes` edge (annotated
   * with `superseded_by`) and turns folding off, so every version is its own
   * row. Backend default false.
   */
  include_superseded?: boolean;
  filters?: {
    context_id?: string;
    scope?: MemoryScope;
    type?: string;
    tags?: string[];
    tags_match?: "any" | "all";
  };
}

// Mirror of backend `FoldedVersions` (schemas.py, #2123): judge-verified older
// versions of a fact folded under the newest one. Ids are newest first.
export interface FoldedVersions {
  count: number;
  memory_ids: string[];
}

// Mirror of backend `MemoryResponse` (schemas.py:186) — one recall result row.
// `score` is present only on recall (null elsewhere).
export interface RecallResultItem {
  memory_id: string;
  summary: string;
  context_summary: string | null;
  type: string;
  importance: number;
  scope: MemoryScope;
  created_at: string;
  client: string;
  tags: string[];
  context: Record<string, unknown> | null;
  score?: number | null;
  source_uri?: string | null;
  source_type?: string | null;
  /** #1208: set only when include_superseded=true opted a shadowed row back in. */
  superseded_by?: string | null;
  /** #2123: older versions folded under this row; absent when none. */
  folded?: FoldedVersions | null;
}

// Mirror of backend `RecallResponse` (schemas.py:288).
export interface RecallResponse {
  results: RecallResultItem[];
  related_tags?: { tag: string; count: number }[];
  explore_hints?: { memory_id: string; reason: string }[];
}

// Generic over the row shape so callers can specialize when needed.
// Default is `MemoryListItem` (the structurally correct type for
// `GET /api/v1/memory/list`).
export interface MemoryListResponse<TItem = MemoryListItem> {
  memories: TItem[];
  total: number;
  has_more: boolean;
}
