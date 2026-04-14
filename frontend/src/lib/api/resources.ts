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
