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
  // #1376: vend-settings presence indicators. The LLM bundle is write-only —
  // only the flag is listed.
  channel_ids: string[] | null;
  locale: string | null;
  litellm_virtual_key_id: string | null;
  llm_config_present: boolean;
  // #1389: human-readable identity for the list row — the resource's label,
  // the platform team id, and the write-target context's name.
  display_name: string | null;
  external_team_id: string | null;
  context_name: string | null;
}

export interface ConnectorReadiness {
  ready: boolean;
  missingChannels: boolean;
  missingLlm: boolean;
}

// #1388: vend-readiness rule, mirrored from what the worker actually needs
// to serve a connector today: an ingest channel selection plus a stored BYO
// LLM bundle. litellm_virtual_key_id is stored but not vended yet
// (kagura-bridge#179), so it must NOT count toward readiness — a connector
// with only a virtual key would read "ready" while the worker gets llm=null.
// Single source for every readiness surface (dialog summary, row indicators);
// the #1392 platform-LLM lane changes this rule in exactly one place.
//
// #1426: on managed (hosted SaaS) deployments the shared worker/bridge provides
// the pre-compile LLM, so a per-connector LLM is NOT required — pass
// llmRequired=false (from features.managed_connectors) and a missing LLM stops
// counting against readiness. Defaults true so OSS/self-host is unchanged.
export function connectorReadiness(
  c: WorkspaceConnectorSummary,
  opts: { llmRequired?: boolean } = {},
): ConnectorReadiness {
  const { llmRequired = true } = opts;
  const missingChannels = !c.channel_ids?.length;
  const missingLlm = llmRequired && !c.llm_config_present;
  return {
    ready: !missingChannels && !missingLlm,
    missingChannels,
    missingLlm,
  };
}

// #1389: the one human-readable name for a connector, shared by every
// surface that labels one (list row today; dialog titles/toasts as they
// adopt names) so row and dialog can never disagree on what a connector
// is called.
export function connectorDisplayName(c: WorkspaceConnectorSummary): string {
  return (
    c.display_name ||
    c.external_team_id ||
    c.connector_type.charAt(0).toUpperCase() + c.connector_type.slice(1)
  );
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

/**
 * PATCH body for connector vend settings (#1376). True PATCH semantics:
 * omit a field to leave it untouched; explicit null clears it.
 */
export interface UpdateConnectorSettingsRequest {
  channel_ids?: string[] | null;
  litellm_virtual_key_id?: string | null;
  // Write-only BYO LLM bundle — never returned (a presence flag is).
  llm_config?: Record<string, unknown> | null;
  locale?: string | null;
  // #1428: re-point the write-target context in place (no delete→recreate).
  // Must be a live context in this workspace; null clears the binding.
  context_id?: string | null;
}

export interface UpdateConnectorSettingsResponse {
  connector_id: string;
  channel_ids: string[] | null;
  litellm_virtual_key_id: string | null;
  llm_config_present: boolean;
  locale: string | null;
  config_version: number;
  context_id: string | null;
}

export async function updateConnectorSettings(
  connectorId: string,
  patch: UpdateConnectorSettingsRequest,
  // Optimistic-concurrency guard: version the caller's snapshot came from.
  expectedConfigVersion?: number,
): Promise<UpdateConnectorSettingsResponse> {
  return apiClient.patch<UpdateConnectorSettingsResponse>(
    `/api/v1/workspace-connectors/${connectorId}`,
    {
      ...patch,
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
