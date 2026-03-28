/**
 * API Keys Client
 *
 * Functions for interacting with Kagura API Keys management endpoints
 * Issue #655 - API Key Management Page
 */

import { apiClient } from './base';
import type {
  APIKey,
  APIKeyCreateRequest,
  APIKeyCreateResponse,
  APIKeyStats,
} from '../types/api-key';

/**
 * Get list of all API keys (Admin only)
 */
export async function getAPIKeys(): Promise<APIKey[]> {
  return apiClient.get<APIKey[]>('/api/v1/config/api-keys');
}

/**
 * Create a new API key (Admin only)
 *
 * IMPORTANT: The plaintext api_key is ONLY returned once.
 * The client MUST save it immediately - it cannot be retrieved again.
 */
export async function createAPIKey(
  data: APIKeyCreateRequest
): Promise<APIKeyCreateResponse> {
  return apiClient.post<APIKeyCreateResponse>('/api/v1/config/api-keys', data);
}

/**
 * Revoke an API key (Admin only)
 *
 * Soft delete - key remains in database for audit trail but cannot be used.
 */
export async function revokeAPIKey(keyId: number): Promise<void> {
  return apiClient.post<void>(`/api/v1/config/api-keys/${keyId}/revoke`, {});
}

/**
 * Permanently delete an API key (Admin only)
 *
 * Hard delete - key and all associated data are permanently removed from database.
 * Use revokeAPIKey() if you want to preserve audit history.
 */
export async function deleteSystemAPIKey(keyId: number): Promise<void> {
  return apiClient.delete<void>(`/api/v1/config/api-keys/${keyId}`);
}

/**
 * Regenerate an API key (Admin only)
 *
 * Issue #169: Revokes old key and creates new one with same name.
 * WARNING: Old key is immediately invalidated.
 *
 * IMPORTANT: The plaintext api_key is ONLY returned once.
 * The client MUST save it immediately - it cannot be retrieved again.
 */
export async function regenerateAPIKey(keyId: number): Promise<APIKeyCreateResponse> {
  return apiClient.post<APIKeyCreateResponse>(`/api/v1/config/api-keys/${keyId}/regenerate`, {});
}

/**
 * Get usage statistics for an API key (Admin only)
 *
 * @param keyId - Database ID of the key
 * @param days - Number of days to retrieve (1-90, default: 30)
 */
export async function getAPIKeyStats(
  keyId: number,
  days: number = 30
): Promise<APIKeyStats> {
  const searchParams = new URLSearchParams({ days: days.toString() });
  return apiClient.get<APIKeyStats>(
    `/api/v1/config/api-keys/${keyId}/stats?${searchParams.toString()}`
  );
}

/**
 * Helper: Format date for display
 */
export function formatDate(isoString: string | null): string {
  if (!isoString) return 'Never';

  const date = new Date(isoString);
  return date.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * Helper: Format relative time (e.g., "2 days ago")
 */
export function formatRelativeTime(isoString: string | null): string {
  if (!isoString) return 'Never';

  const date = new Date(isoString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  if (diffDay > 30) {
    return formatDate(isoString);
  } else if (diffDay > 0) {
    return `${diffDay} day${diffDay > 1 ? 's' : ''} ago`;
  } else if (diffHour > 0) {
    return `${diffHour} hour${diffHour > 1 ? 's' : ''} ago`;
  } else if (diffMin > 0) {
    return `${diffMin} minute${diffMin > 1 ? 's' : ''} ago`;
  } else {
    return 'Just now';
  }
}

/**
 * Helper: Check if key is expired
 */
export function isExpired(expiresAt: string | null): boolean {
  if (!expiresAt) return false; // No expiration
  return new Date(expiresAt) < new Date();
}

/**
 * Helper: Get badge color for status
 */
export function getStatusColor(status: string): string {
  switch (status) {
    case 'active':
      return 'green';
    case 'revoked':
      return 'gray';
    case 'expired':
      return 'red';
    default:
      return 'gray';
  }
}
