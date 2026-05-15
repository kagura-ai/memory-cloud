/**
 * Signup Gate Admin API Client
 *
 * Issue #358 Phase 1: admin-configurable signup gate.
 * Issue #655: extended to support Google OAuth allowlist alongside GitHub.
 *
 * Mode values in the client mirror the backend literal union exactly —
 * Phase 2 modes (github_sponsors / both) are present in the type so the UI
 * can render a disabled option; the admin page blocks selecting them and
 * the backend returns 400 if someone bypasses the UI.
 */

import { apiClient } from "./base";

export type SignupGateMode = "manual" | "github_sponsors" | "both";

export type SignupGateProvider = "github" | "google";

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
  // Issue #655 canonical provider-aware fields.
  provider: SignupGateProvider;
  subject_id: string;
  subject_label: string;
  // Legacy fields kept by the backend for pre-#655 admin tooling — they are
  // present on every row (filled with "_email_" markers on provider='google'
  // rows). Read-only here; UI prefers `subject_label` for display.
  github_user_id: string;
  github_username: string;
  source: "manual" | "github_sponsors";
  state: "active" | "grace" | "revoked";
  added_by_user_id: string | null;
}

/**
 * Discriminated union for the add-allowlist payload (Issue #655).
 *
 * Backend accepts:
 *   - `{provider: "github", github_username}` — canonical GitHub
 *   - `{github_username}` — pre-#655 legacy (provider defaults to github)
 *   - `{provider: "google", email}` — Google
 *
 * UI always sends the canonical `{provider, ...}` shape for new entries.
 */
export type SignupAllowlistAddPayload =
  | { provider: "github"; github_username: string }
  | { provider: "google"; email: string };

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
  payload: SignupAllowlistAddPayload,
): Promise<SignupAllowlistEntry> {
  return apiClient.post<SignupAllowlistEntry>(
    "/api/v1/admin/signup-gate/allowlist",
    payload,
  );
}

export async function removeSignupAllowlistEntry(
  entryId: string,
): Promise<void> {
  await apiClient.delete(`/api/v1/admin/signup-gate/allowlist/${entryId}`);
}
