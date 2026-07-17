/**
 * Workspace Connectors API client (Spec 2026-06-02).
 *
 * ai-worker chat-ingest connector registration: list / create / delete, plus
 * the Slack OAuth install handoff. Shapes mirror the backend
 * (api/routes/workspace_connectors.py + connectors_slack.py).
 */

import { apiClient, API_BASE_URL } from "./base";

export type ConnectorType = "slack" | "discord" | "teams";

export interface WorkerRuntimeConfig {
  buffer: { ttl_seconds: number; max_len: number };
  flush: {
    silence_seconds: number;
    volume_tokens: number;
    max_tracked_topics: number;
  };
  supervisor: {
    tick_seconds: number;
    shutdown_flush_timeout_seconds: number;
  };
  lifecycle: {
    deletion_mode: "forget" | "redact";
    redacted_summary: string;
    dormant_summary: string;
  };
  continuity: {
    time_window_minutes: number;
    semantic_threshold: number;
    semantic_check_enabled: boolean;
  };
  vision_enabled: boolean;
  mention_answer_enabled: boolean;
  answer_relevance_threshold: number;
  answer_timeout_sec: number;
  memory_link_template: string | null;
  entity_extraction_enabled: boolean;
  entity_max: number;
}

/** One row in the connectors list (GET /workspace-connectors). */
export interface WorkspaceConnectorSummary {
  connector_id: string;
  connector_type: ConnectorType;
  app_key: string;
  resource_id: string;
  context_id: string | null;
  config_version: number;
  created_at: string;
  created_by: string | null;
  runtime: WorkerRuntimeConfig;
}

/** Create request — the registration flow fields are all optional. */
export interface CreateConnectorRequest {
  connector_type: ConnectorType;
  resource_id: string;
  display_name?: string;
  oauth_tokens?: Record<string, unknown>;
  auto_create_context_name?: string;
  context_id?: string;
  llm_config?: Record<string, unknown>;
  channel_ids?: unknown[];
  locale?: string;
  pii_guardrail_config?: Record<string, unknown>;
  external_team_id?: string;
  slack_install_handle?: string;
  app_key?: string;
  runtime?: Partial<WorkerRuntimeConfig>;
}

/** Create response — token + KMC key are shown exactly once. */
export interface CreateConnectorResponse {
  connector_id: string;
  connector_type: ConnectorType;
  app_key: string;
  resource_id: string;
  context_id: string | null;
  token_id: number;
  token: string;
  kmc_api_key: string | null;
  quota_events_per_hour: number;
  idempotency_key_prefix: string;
}

/** Non-secret summary of a pending Slack install (after OAuth callback). */
export interface SlackPendingInstall {
  team_id: string;
  team_name: string | null;
  installing_admin_user_id: string | null;
  app_key: string;
}

export interface AvailableWorkerApp {
  platform: ConnectorType;
  app_key: string;
  display_name: string;
}

export interface UpdateConnectorRuntimeResponse {
  connector_id: string;
  // Effective config (worker defaults when the stored block was cleared).
  runtime: WorkerRuntimeConfig;
  // false = cleared/NULL row (worker built-in defaults apply).
  stored: boolean;
  config_version: number;
}

export async function listConnectors(): Promise<WorkspaceConnectorSummary[]> {
  return apiClient.get<WorkspaceConnectorSummary[]>(
    "/api/v1/workspace-connectors",
  );
}

export async function listAvailableWorkerApps(): Promise<AvailableWorkerApp[]> {
  return apiClient.get<AvailableWorkerApp[]>(
    "/api/v1/workspace-connectors/available-apps",
  );
}

export async function createConnector(
  data: CreateConnectorRequest,
): Promise<CreateConnectorResponse> {
  return apiClient.post<CreateConnectorResponse>(
    "/api/v1/workspace-connectors",
    data,
  );
}

export async function deleteConnector(connectorId: string): Promise<void> {
  return apiClient.delete<void>(`/api/v1/workspace-connectors/${connectorId}`);
}

export async function updateConnectorRuntime(
  connectorId: string,
  // null clears the stored block back to worker defaults (#1348).
  runtime: WorkerRuntimeConfig | null,
  // Optimistic-concurrency guard: version the caller's snapshot came from.
  // The server 409s on mismatch instead of silently reverting a concurrent
  // change (full-document replacement semantics).
  expectedConfigVersion?: number,
): Promise<UpdateConnectorRuntimeResponse> {
  return apiClient.patch<UpdateConnectorRuntimeResponse>(
    `/api/v1/workspace-connectors/${connectorId}/runtime`,
    {
      runtime,
      ...(expectedConfigVersion != null
        ? { expected_config_version: expectedConfigVersion }
        : {}),
    },
  );
}

export async function getSlackPendingInstall(
  handle: string,
): Promise<SlackPendingInstall> {
  return apiClient.get<SlackPendingInstall>(
    `/api/v1/connectors/slack/pending/${encodeURIComponent(handle)}`,
  );
}

/**
 * Full-page navigation to the Slack OAuth install (the backend 302s to Slack).
 * Uses a real browser navigation — not fetch — so the session cookie rides
 * along and Slack can redirect back to our callback.
 */
export function slackInstallUrl(): string {
  return `${API_BASE_URL}/api/v1/connectors/slack/install`;
}
