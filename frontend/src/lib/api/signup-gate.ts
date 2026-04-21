/**
 * Signup Gate Admin API Client
 *
 * Issue #358 Phase 1: admin-configurable signup gate.
 *
 * Mode values in the client mirror the backend literal union exactly —
 * Phase 2 modes (github_sponsors / both) are present in the type so the UI
 * can render a disabled option; the admin page blocks selecting them and
 * the backend returns 400 if someone bypasses the UI.
 */

import { apiClient } from "./base";

export type SignupGateMode = "manual" | "github_sponsors" | "both";

export interface SignupGateConfig {
  enabled: boolean;
  mode: SignupGateMode;
  github_sponsors_grace_period_days: number;
}

export interface SignupGateConfigUpdate {
  enabled: boolean;
  mode: SignupGateMode;
}

export interface SignupAllowlistEntry {
  id: string;
  github_user_id: string;
  github_username: string;
  source: "manual" | "github_sponsors";
  state: "active" | "grace" | "revoked";
  added_by_user_id: string | null;
}

export async function getSignupGateConfig(): Promise<SignupGateConfig> {
  return apiClient.get<SignupGateConfig>("/api/v1/admin/signup-gate/config");
}

export async function updateSignupGateConfig(
  payload: SignupGateConfigUpdate,
): Promise<SignupGateConfig> {
  return apiClient.put<SignupGateConfig>(
    "/api/v1/admin/signup-gate/config",
    payload,
  );
}

export async function listSignupAllowlist(): Promise<SignupAllowlistEntry[]> {
  return apiClient.get<SignupAllowlistEntry[]>(
    "/api/v1/admin/signup-gate/allowlist",
  );
}

export async function addSignupAllowlistEntry(
  github_username: string,
): Promise<SignupAllowlistEntry> {
  return apiClient.post<SignupAllowlistEntry>(
    "/api/v1/admin/signup-gate/allowlist",
    { github_username },
  );
}

export async function removeSignupAllowlistEntry(
  entryId: string,
): Promise<void> {
  await apiClient.delete(`/api/v1/admin/signup-gate/allowlist/${entryId}`);
}
