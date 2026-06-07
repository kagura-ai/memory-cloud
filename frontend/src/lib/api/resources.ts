/**
 * Workspace Resource List Client
 *
 * Issue #47 — Web UI for resource management.
 *
 * Companion to schemas.ts (per-resource impact + schema) and
 * resource-tokens.ts (token CRUD). This module owns only the
 * workspace-scoped list endpoint.
 */

import { apiClient } from "./base";

/** Single row in the workspace resource list. */
export interface ResourceListItem {
  resource_id: string;
  context_id: string;
  context_name: string;
  context_display_name: string | null;
  token_count: number;
  memory_count: number;
  current_schema_version: number | null;
  /** ISO 8601 UTC — context creation time. */
  created_at: string;
  /** ISO 8601 UTC — max(last_event_at, context.updated_at, context.created_at). */
  updated_at: string;
}

/** Response from GET /api/v1/resources. */
export interface ResourceListResponse {
  resources: ResourceListItem[];
  total: number;
}

/**
 * List all resources in the caller's current workspace.
 * Ordered by most recent activity first.
 */
export async function listResources(): Promise<ResourceListResponse> {
  return apiClient.get<ResourceListResponse>("/api/v1/resources");
}

// ============================================================================
// Issue #326 — Indexer Status
// ============================================================================
//
// Types are hand-mirrored against the pydantic schema in
// `backend/src/api/routes/resource_indexer.py`. The backend carries a
// snapshot test (`test_resource_indexer_openapi_snapshot.py`) that fails
// in CI when the OpenAPI shape drifts — that's the signal to update these
// types. Switching to a generator is tracked as a separate DX epic.

/** Lifecycle states of an indexer run, matching the backend Literal. */
export type IndexerJobStatus = "idle" | "queued" | "running" | "failed";

/**
 * Reasons the indexer may record when it skips a run. Extending this union
 * also means extending the backend Literal and re-running the OpenAPI
 * snapshot — keep the two in lock-step.
 */
export type IndexerSkippedReason =
  | "no_pending_events"
  | "schema_not_found"
  | "context_not_found"
  | "empty_valid_points";

export interface IndexerStateMetrics {
  applied_upserts: number;
  applied_deletes: number;
  errors: number;
  /**
   * Non-null only when the last run was skipped. Stale reasons after a
   * successful run are suppressed server-side — the UI can show the Alert
   * unconditionally when this is set.
   */
  skipped_reason: IndexerSkippedReason | null;
}

export interface IndexerState {
  job_status: IndexerJobStatus;
  /** ISO-8601 UTC, null when the indexer has never run. */
  last_run_at: string | null;
  /** ISO-8601 UTC, null when no run is scheduled. */
  next_run_at: string | null;
  active_version: number;
  last_offset: number;
  /** Server-computed `now - last_run_at` in seconds; null when never run. */
  lag_seconds: number | null;
  metrics: IndexerStateMetrics;
}

export interface ResourceEventItem {
  id: number;
  op: "upsert" | "delete";
  doc_id: string;
  /** NULL version is valid for delete-all-versions (Issue #262). */
  version: number | null;
  /** ISO-8601 UTC; null only if the DB row has no created_at (shouldn't happen). */
  created_at: string | null;
}

/** Response shape from `GET /api/v1/resources/{id}/indexer-status`. */
export interface IndexerStatusResponse {
  resource_id: string;
  /**
   * `null` means the indexer has never run for this resource/context. The
   * UI branches on this sentinel rather than inferring emptiness from
   * individual state fields.
   */
  state: IndexerState | null;
  /** Newest first, server-capped at 5. */
  recent_events: ResourceEventItem[];
}

/**
 * Fetch indexer runtime state and recent ingest events for a resource.
 * The slug is URL-encoded for safety, but the backend's `Context.resource_id`
 * column is constrained to a single path segment of `[a-z0-9_-]+` — `/`
 * is not a valid character. The encode call is defensive; callers should
 * pass a backend-validated slug.
 */
export async function getIndexerStatus(
  resourceId: string,
): Promise<IndexerStatusResponse> {
  return apiClient.get<IndexerStatusResponse>(
    `/api/v1/resources/${encodeURIComponent(resourceId)}/indexer-status`,
  );
}

// ============================================================================
// Issue #316 — Resource event data browser (Data tab)
// ============================================================================
//
// Types are hand-mirrored against the pydantic ResourceEventsResponse in
// `backend/src/api/routes/resources.py`. Keep in lock-step with that schema.

/** A single ingest event row for the Data tab. */
export interface ResourceEventRecord {
  /**
   * BigInt append-only event id; also the keyset cursor value. Serialized as
   * a string end-to-end so values above 2^53-1 keep full precision in JS.
   */
  id: string;
  op: "upsert" | "delete";
  doc_id: string;
  /** NULL version is valid for delete-all-versions (Issue #262). */
  version: number | null;
  idempotency_key: string | null;
  importance: number;
  /** ISO-8601 UTC (Z-suffixed). */
  created_at: string;
  /**
   * JSONB payload. `null` for delete ops, OR when omitted because it
   * exceeded the inline size cap — branch on `payload_truncated` to tell
   * the two cases apart.
   */
  payload: Record<string, unknown> | null;
  event_metadata: Record<string, unknown> | null;
  /** Serialized payload size in bytes (0 when null). */
  payload_bytes: number;
  /** True when the payload was omitted because it exceeded the inline cap. */
  payload_truncated: boolean;
}

/** Response shape from `GET /api/v1/resources/{id}/events`. */
export interface ResourceEventsResponse {
  events: ResourceEventRecord[];
  /** Opaque cursor for the next page; `null` on the last page. */
  next_cursor: string | null;
}

/** Fixed, server-allowed filters for the events browser. No JSONB DSL. */
export interface ResourceEventFilters {
  op?: "upsert" | "delete";
  doc_id?: string;
  version?: number;
  /** ISO-8601 timestamp lower bound (inclusive). */
  since?: string;
  limit?: number;
  cursor?: string;
}

/**
 * Browse ingested events for a resource, newest first (cursor-paginated).
 * The slug is URL-encoded defensively; the backend constrains it to a single
 * `[a-z0-9_-]+` path segment.
 */
export async function listResourceEvents(
  resourceId: string,
  filters: ResourceEventFilters = {},
): Promise<ResourceEventsResponse> {
  const params = new URLSearchParams();
  if (filters.op) params.set("op", filters.op);
  if (filters.doc_id) params.set("doc_id", filters.doc_id);
  if (filters.version !== undefined)
    params.set("version", String(filters.version));
  if (filters.since) params.set("since", filters.since);
  if (filters.limit !== undefined) params.set("limit", String(filters.limit));
  if (filters.cursor) params.set("cursor", filters.cursor);
  const qs = params.toString();
  return apiClient.get<ResourceEventsResponse>(
    `/api/v1/resources/${encodeURIComponent(resourceId)}/events${
      qs ? `?${qs}` : ""
    }`,
  );
}
