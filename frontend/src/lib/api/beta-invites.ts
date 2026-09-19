/**
 * Beta invite links API client (#1582; backend contract from #1581).
 *
 * A signed-in user mints a one-time `/join/{token}` URL that lets one new
 * person through the closed signup gate. Every route 404s while the
 * deployment has the feature off (`features.beta_invites` in
 * `GET /api/v1/system/info`), so callers gate on that flag before asking.
 *
 * The invite URL and its token are credentials: the plaintext URL comes back
 * exactly once, from `createBetaInvite`. Never log or persist either.
 */

import { apiClient } from "./base";

export type BetaInviteStatus = "active" | "redeemed" | "expired" | "revoked";

export interface BetaInvite {
  id: string;
  status: BetaInviteStatus;
  created_at: string;
  expires_at: string;
  redeemed_at: string | null;
  revoked_at: string | null;
}

export interface BetaInviteSummary {
  /** `null` = unlimited (system admin). */
  quota: number | null;
  used: number;
  /** `null` = unlimited (system admin). */
  remaining: number | null;
  invites: BetaInvite[];
}

export interface BetaInviteCreated {
  id: string;
  /** Plaintext invite URL — returned only here, never again. */
  url: string;
  expires_at: string;
}

export interface BetaInvitePreview {
  valid: true;
  expires_at: string;
}

/** The caller's quota and invites. */
export async function getMyBetaInvites(): Promise<BetaInviteSummary> {
  return await apiClient.get<BetaInviteSummary>("/api/v1/beta-invites/me");
}

/** Mint an invite. Rejects with a 409 `ApiError` at the quota. */
export async function createBetaInvite(): Promise<BetaInviteCreated> {
  return await apiClient.post<BetaInviteCreated>("/api/v1/beta-invites");
}

/** Revoke an active invite. Rejects with a 409 `ApiError` once redeemed. */
export async function revokeBetaInvite(id: string): Promise<void> {
  await apiClient.delete(`/api/v1/beta-invites/${encodeURIComponent(id)}`);
}

/**
 * Public preview for the `/join/{token}` landing page. Rejects with a 404
 * `ApiError` for an unknown or revoked token (and for every token while the
 * feature is off), 410 for an expired or redeemed one.
 */
export async function previewBetaInvite(
  token: string,
): Promise<BetaInvitePreview> {
  return await apiClient.get<BetaInvitePreview>(
    `/api/v1/beta-invites/${encodeURIComponent(token)}/preview`,
  );
}
