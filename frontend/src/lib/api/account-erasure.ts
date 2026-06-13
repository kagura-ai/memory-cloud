/**
 * Self-serve account erasure API client (Issue #953).
 *
 * Thin wrapper over the SessionUser-authenticated `/me/account/erasure-*`
 * endpoints. The backend (AccountErasureService, #360/#469/#486/#489) is the
 * source of truth; this only shapes the request/response and rides the session
 * cookie via `apiClient` (credentials: "include").
 *
 * Two confirmation channels (#469):
 *  - Password-auth users: `requestErasure()` returns a `confirm_token` in the
 *    body; the user re-enters their password and we POST both to confirm.
 *  - OAuth users: `confirm_token` is null; the token is emailed as a one-time
 *    link to `/account/erasure/confirm?token=…`, which calls `confirmErasure`
 *    with the token (no password).
 *
 * The confirm token is sensitive — never log it.
 */
import { apiClient } from "@/lib/api/base";

const BASE = "/api/v1/me/account/erasure-request";
const CONFIRM = "/api/v1/me/account/erasure-confirm";

/** Response from POST /me/account/erasure-request. */
export interface ErasureRequestCreateResponse {
  request_id: string;
  status: string;
  requested_at: string;
  /** Populated for password-auth users only; null for OAuth (emailed instead). */
  confirm_token: string | null;
}

/** Lifecycle view returned by confirm / cancel / get-active. */
export interface ErasureRequestState {
  request_id: string;
  status: string;
  is_self_service: boolean;
  requested_at: string;
  confirmed_at: string | null;
  scheduled_for: string | null;
  started_at: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
  failure_reason: string | null;
}

/** Create a pending erasure request and (for password users) get a one-time token. */
export function requestErasure(): Promise<ErasureRequestCreateResponse> {
  return apiClient.post<ErasureRequestCreateResponse>(BASE);
}

/**
 * Confirm a pending request, starting the 7-day cooling-off period.
 * `password` is required for password-auth users and omitted for OAuth users.
 */
export function confirmErasure(
  token: string,
  password?: string,
): Promise<ErasureRequestState> {
  const body: { token: string; password?: string } = { token };
  if (password) body.password = password;
  return apiClient.post<ErasureRequestState>(CONFIRM, body);
}

/** Cancel (undo) the active erasure request during cooling-off. */
export function cancelErasure(): Promise<ErasureRequestState> {
  return apiClient.delete<ErasureRequestState>(BASE);
}

/** Fetch the caller's active erasure request, or null if none. */
export function getActiveErasureRequest(): Promise<ErasureRequestState | null> {
  return apiClient.get<ErasureRequestState | null>(BASE);
}

/**
 * Active erasure lifecycle stage, mirroring the backend status the
 * GET /erasure-request endpoint surfaces ("pending" | "cooling_off" |
 * "in_progress"; terminal states are not returned). Drives which danger-zone
 * UI to render:
 *   - "none"        → no active request; show the delete control.
 *   - "pending"     → requested, awaiting confirmation; cancellable.
 *   - "cooling_off" → confirmed, 7-day window running; cancellable.
 *   - "in_progress" → deletion executing; NOT cancellable.
 *
 * Keyed on the authoritative `status` rather than inferred from timestamps so
 * `in_progress` (which still carries confirmed_at/scheduled_for) is not
 * mistaken for a cancellable cooling-off request.
 */
export type ErasureStage = "none" | "pending" | "cooling_off" | "in_progress";

export function erasureStage(state: ErasureRequestState | null): ErasureStage {
  switch (state?.status) {
    case "pending":
      return "pending";
    case "cooling_off":
      return "cooling_off";
    case "in_progress":
      return "in_progress";
    default:
      // null, or a terminal status the endpoint shouldn't surface anyway.
      return "none";
  }
}
