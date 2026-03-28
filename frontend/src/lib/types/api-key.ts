/**
 * API Key types for Kagura Cloud Web UI
 * Issue #655 - API Key Management Page
 */

export type APIKeyStatus = "active" | "revoked" | "expired";

export interface APIKey {
  id: number;
  key_prefix: string; // First 16 characters (e.g., "kagura_abc123...")
  name: string; // Friendly name
  user_id: string; // Owner OAuth2 sub
  created_at: string; // ISO timestamp
  last_used_at: string | null; // ISO timestamp or null
  revoked_at: string | null; // ISO timestamp or null
  expires_at: string | null; // ISO timestamp or null
  status: APIKeyStatus;
}

export interface APIKeyCreateRequest {
  name: string; // Friendly name (required)
  expires_days: number | null; // 30, 90, 365, or null for no expiration
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
  keyId: number | null;
  keyName: string | null;
}

export interface RevokeDialogState {
  isOpen: boolean;
  keyId: number | null;
  keyName: string | null;
}

export interface DeleteDialogState {
  isOpen: boolean;
  keyId: number | null;
  keyName: string | null;
}
