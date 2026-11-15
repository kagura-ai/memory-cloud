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

/**
 * #1734: an allowlist row can also record an email sign-up (`provider='email'`,
 * written when a beta invite is redeemed by email). Read rows only: the admin
 * add form never creates one.
 */
export type SignupAllowlistProvider = SignupGateProvider | "email";

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
  provider: SignupAllowlistProvider;
  subject_id: string;
  subject_label: string;
  // Legacy fields kept by the backend for pre-#655 admin tooling — they are
  // present on every row (filled with "_email_" markers on provider='google'
  // rows). Read-only here; UI prefers `subject_label` for display.
  github_user_id: string;
  github_username: string;
  source: "manual" | "github_sponsors" | "beta_invite";
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

/**
 * One page of the allowlist (#2831), newest first. `total` counts every row
 * that matches the filters, not the page.
 */
export interface SignupAllowlistPage {
  items: SignupAllowlistEntry[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * Parameters of the allowlist list (#2831). `limit` is 1 to 200 (the server's
 * default is 50); `search` is a case-insensitive substring of `subject_label`;
 * the other filters are exact. The server refuses a bad value with 422.
 */
export interface SignupAllowlistListParams {
  limit?: number;
  offset?: number;
  search?: string;
  provider?: SignupAllowlistProvider;
  source?: SignupAllowlistEntry["source"];
  state?: SignupAllowlistEntry["state"];
}

export async function listSignupAllowlist(
  params: SignupAllowlistListParams = {},
): Promise<SignupAllowlistPage> {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  if (params.search) query.set("search", params.search);
  if (params.provider) query.set("provider", params.provider);
  if (params.source) query.set("source", params.source);
  if (params.state) query.set("state", params.state);
  const suffix = query.toString();
  return apiClient.get<SignupAllowlistPage>(
    `/api/v1/admin/signup-gate/allowlist${suffix ? `?${suffix}` : ""}`,
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
