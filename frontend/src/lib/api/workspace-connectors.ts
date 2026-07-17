/**
 * Workspace Connectors API client (Spec 2026-06-02).
 *
 * ai-worker chat-ingest connector registration: list / create / delete, plus
 * the Slack OAuth install handoff. Shapes mirror the backend
 * (api/routes/workspace_connectors.py + connectors_slack.py).
 */

import { apiClient, API_BASE_URL } from "./base";

export type ConnectorType = "slack" | "discord" | "teams";

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
