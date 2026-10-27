/**
 * API Key types for Kagura Cloud Web UI
 * Issue #655 - API Key Management Page
 */

export type APIKeyStatus = "active" | "revoked" | "expired";

export interface APIKey {
  id: string; // Public id: "akey_" + 22 base62 chars (#1008)
  key_prefix: string; // First 16 characters (e.g., "kagura_abc123...")
  name: string; // Friendly name
  user_id: string; // Owner OAuth2 sub
  created_at: string; // ISO timestamp
  last_used_at: string | null; // ISO timestamp or null
  revoked_at: string | null; // ISO timestamp or null
  expires_at: string | null; // ISO timestamp or null
  status: APIKeyStatus;
  // #2129: the key's MCP default context — a context-scoped tool called
  // without context_id runs in it (?context= on the URL wins). The name is
  // null when none is set OR the owner can no longer read the context.
  default_context_id: string | null;
  default_context_name: string | null;
  // #2299: set when the key is a connector's shared key (#1952) — the
  // `connector:` name is display only.
  connector_id: string | null;
}

export interface APIKeyCreateRequest {
  name: string; // Friendly name (required)
  // Days until expiry (1-3650). 0 = never expires (explicit opt-in);
  // null/omitted = server default (365 days) — see #1537.
  expires_days: number | null;
  // #2129: optional MCP default context (404 "Context not found" when the
  // user cannot read it — no key is created).
  default_context_id?: string | null;
}

/** #2129: body of PATCH /api/v1/config/api-keys/{key_id}; null clears. */
export interface APIKeyUpdateRequest {
  default_context_id: string | null;
}

export interface APIKeyCreateResponse extends APIKey {
  api_key: string; // Plaintext key (ONLY shown once!)
}

export interface DailyStats {
  date: string; // ISO date (YYYY-MM-DD)
  count: number; // Number of requests on this date
}

export interface APIKeyStats {
  total_requests: number;
  daily_stats: DailyStats[];
  period_start: string; // ISO date
  period_end: string; // ISO date
}

// UI-only helper types
export interface CreateDialogState {
  isOpen: boolean;
  createdKey: APIKeyCreateResponse | null; // Store created key for one-time display
}

export interface StatsDialogState {
  isOpen: boolean;
  keyId: string | null;
  keyName: string | null;
}

export interface RevokeDialogState {
  isOpen: boolean;
  keyId: string | null;
  keyName: string | null;
}

export interface DeleteDialogState {
  isOpen: boolean;
  keyId: string | null;
  keyName: string | null;
}
