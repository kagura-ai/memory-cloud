/**
 * Billing handoff API (#1093 / #1118).
 *
 * Mints an owner-scoped, short-lived Ed25519 token to start a billing session
 * on the external billing service without that service re-implementing auth.
 *
 * - When the deployment sets PAYMENT_PUBLIC_BASE_URL, the response includes a
 *   ready-to-use `url` ({base}/enter?t={token}); otherwise `url` is null and the
 *   caller holds only the raw token.
 * - When the handoff signing key is unconfigured the backend returns 503
 *   (BILLING-002) — surface that as "billing not available on this deployment"
 *   rather than a hard error. memory-cloud stays Stripe-agnostic (#1096); plan
 *   changes flow through this signed handoff, never a self-serve plan mutation.
 */
import { apiClient } from "@/lib/api/base";

export interface BillingHandoffResponse {
  token: string;
  token_type: string;
  kid: string;
  jti: string;
  expires_at: string;
  /** Ready-to-use redirect URL, or null when the billing host base is unset. */
  url: string | null;
}

/** Mint an owner handoff token for the given workspace (owner + session only). */
export async function mintBillingHandoff(
  workspaceId: string,
): Promise<BillingHandoffResponse> {
  return apiClient.post<BillingHandoffResponse>("/api/v1/billing/handoff", {
    workspace_id: workspaceId,
  });
}
