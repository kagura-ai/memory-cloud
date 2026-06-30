/**
 * Secret Store Client (#1134, server #1128)
 *
 * Management surface for the zero-knowledge secret store
 * (`/api/v1/config/secrets/*`). The server holds only age public recipient keys
 * + opaque ciphertext and NEVER decrypts.
 *
 * SECURITY INVARIANT: this client exposes ONLY the management/revoke endpoints
 * (list secrets, list/approve/revoke recipient pubkeys, revoke grants, verify
 * the audit chain). It deliberately does NOT wrap `POST /` (put) or
 * `POST /fetch` (get) — encrypt/decrypt + ciphertext handling live in the
 * `kagura secret` CLI/SDK so the browser never holds a private key, plaintext,
 * or ciphertext.
 */

import { apiClient } from "./base";

/** Secret metadata — never includes the value. */
export interface SecretMeta {
  name: string;
  status: string;
  rotation_needed: boolean;
  current_version: number | null;
  grant_count: number;
  created_at: string;
  updated_at: string | null;
}

export type SecretPubkeyStatus = "pending" | "active" | "revoked";

/** Recipient pubkey metadata (the pubkey is public; no private material). */
export interface SecretPubkey {
  id: string;
  identity_id: string;
  pubkey: string;
  fingerprint: string;
  label: string | null;
  status: SecretPubkeyStatus;
  created_at: string;
  attested_at: string | null;
  revoked_at: string | null;
}

/** Result of recomputing the workspace's tamper-evident audit chain. */
export interface AuditVerifyResult {
  valid: boolean;
  entries: number | null;
  head: string | null;
  broken_at: number | null;
  reason: string | null;
}

const BASE = "/api/v1/config/secrets";

/** List secret names + metadata (owner/admin). Never returns values. */
export function listSecrets(): Promise<SecretMeta[]> {
  return apiClient.get<SecretMeta[]>(BASE);
}

/** List all recipient pubkeys in the workspace (owner/admin approval console). */
export function listSecretPubkeys(): Promise<SecretPubkey[]> {
  return apiClient.get<SecretPubkey[]>(`${BASE}/pubkeys`);
}

/** Owner-approve a pending pubkey → active (the TOFU trust gate). */
export function approveSecretPubkey(pubkeyId: string): Promise<SecretPubkey> {
  return apiClient.post<SecretPubkey>(
    `${BASE}/pubkeys/${pubkeyId}/approve`,
    {},
  );
}

/** Owner-revoke a pubkey; revokes its grants and flags rotation on affected secrets. */
export function revokeSecretPubkey(pubkeyId: string): Promise<SecretPubkey> {
  return apiClient.post<SecretPubkey>(`${BASE}/pubkeys/${pubkeyId}/revoke`, {});
}

/** Revoke one recipient's grant on a secret; sets rotation_needed (revoke ≠ un-share). */
export function revokeSecretGrant(
  name: string,
  recipientPubkeyId: string,
): Promise<SecretMeta> {
  return apiClient.post<SecretMeta>(`${BASE}/revoke-grant`, {
    name,
    recipient_pubkey_id: recipientPubkeyId,
  });
}

/** Recompute + verify the workspace's secret-access audit chain (owner/admin). */
export function verifySecretAudit(): Promise<AuditVerifyResult> {
  return apiClient.get<AuditVerifyResult>(`${BASE}/audit/verify`);
}
