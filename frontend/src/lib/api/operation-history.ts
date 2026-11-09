import { apiClient } from "./base";

export const HISTORY_OPERATIONS = [
  "memory.created", "memory.updated", "memory.deleted", "memory.restored",
  "context.created", "context.updated", "context.deleted", "context.restored",
  "member.invited", "member.added", "member.removed", "member.role_changed", "member.access_changed",
  "credential.issued", "credential.revoked", "integration.created", "integration.updated",
  "integration.deleted", "sleep.completed", "sleep.failed", "sleep.undo",
] as const;

export interface HistoryItem {
  id: string;
  occurred_at: string;
  context_id: string | null;
  actor_user_id: string | null;
  actor_name: string | null;
  subject_user_id: string | null;
  principal_kind: "session" | "api_key" | "oauth" | "resource_token" | "management_grant" | "system" | "unknown";
  credential_id: string | null;
  client_id: string | null;
  agent_id: string | null;
  surface: string;
  operation: typeof HISTORY_OPERATIONS[number];
  target_type: string;
  target_id: string | null;
  outcome: string;
  count: number;
  changed_fields: string[];
}

export interface HistoryPage {
  items: HistoryItem[];
  total: number;
  total_capped: boolean;
  next_cursor: string | null;
}

export function getOperationHistory(workspaceId: string, params: URLSearchParams, signal: AbortSignal) {
  return apiClient.get<HistoryPage>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/operation-history?${params}`,
    { signal, cache: "no-store" },
  );
}
