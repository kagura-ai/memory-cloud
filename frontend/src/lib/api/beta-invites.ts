/**
 * Beta invite links API client (#1582, #1595; backend contract from #1581).
 *
 * A signed-in user mints a one-time `/join/{token}` URL that lets one new
 * person through the closed signup gate. Every route 404s while the
 * deployment has the feature off (`features.beta_invites` in
 * `GET /api/v1/system/info`), so callers gate on that flag before asking.
 *
 * The invite URL and its token are credentials: the plaintext URL comes back
 * exactly once, from `createBetaInvite` or `reissueBetaInvite`. Never log or
 * persist either. An invite's `label` and `redeemed_email` are personal data
 * meant for the inviter's eyes — render them, never log them.
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
  /** The inviter's own note (≤ 100 chars), or `null`. */
  label: string | null;
  /**
   * The admitted account's current e-mail — only on a `redeemed` invite, and
   * only while that account exists (`null` again once it is erased).
   */
  redeemed_email: string | null;
}

export interface BetaInviteSummary {
  /** `null` = unlimited (system admin). */
  quota: number | null;
  /** Slots occupied: always `active + redeemed`. */
  used: number;
  /** Unused links that still work. */
  active: number;
  /** Links someone signed up with. */
  redeemed: number;
  /** `null` = unlimited (system admin). */
  remaining: number | null;
  invites: BetaInvite[];
}

export interface BetaInviteCreated {
  id: string;
  /** Plaintext invite URL — returned only here, never again. */
  url: string;
  expires_at: string;
  label: string | null;
}

export interface BetaInvitePreview {
  valid: true;
  expires_at: string;
}

/** The caller's quota and invites. */
export async function getMyBetaInvites(): Promise<BetaInviteSummary> {
  return await apiClient.get<BetaInviteSummary>("/api/v1/beta-invites/me");
}

/**
 * Mint an invite, optionally labelled. Rejects with a 409 `ApiError` at the
 * quota. Without a label no body is sent at all — the pre-#1595 request.
 */
export async function createBetaInvite(
  label?: string | null,
): Promise<BetaInviteCreated> {
  const trimmed = label?.trim();
  if (!trimmed) {
    return await apiClient.post<BetaInviteCreated>("/api/v1/beta-invites");
  }
  return await apiClient.post<BetaInviteCreated>("/api/v1/beta-invites", {
    label: trimmed,
  });
}

/**
 * Replace an `active` or `expired` invite with a fresh link carrying the same
 * label (the old link stops working). Returns the new plaintext URL — once.
 * Rejects with a 409 `ApiError` whose `error` is `BETA-INVITE-002` (already
 * used), `BETA-INVITE-003` (already revoked — e.g. a double-click; nothing was
 * minted) or `BETA-INVITE-001` (an expired invite, and the caller is at the
 * quota).
 */
export async function reissueBetaInvite(
  id: string,
): Promise<BetaInviteCreated> {
  return await apiClient.post<BetaInviteCreated>(
    `/api/v1/beta-invites/${encodeURIComponent(id)}/reissue`,
  );
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
