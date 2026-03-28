/**
 * Resource Tokens Client
 *
 * Functions for interacting with Resource Token management endpoints
 * Issue #242 - Resource Token Management UI
 */

import { apiClient } from './base';

/**
 * Resource Token interface (metadata only, no plaintext)
 */
export interface ResourceToken {
  id: number;
  resource_id: string;
  description: string | null;
  quota_events_per_hour: number;
  created_by: string | null;
  created_at: string;
  last_used_at: string | null;
  is_active: boolean;
  status: 'active' | 'revoked';
}

/**
 * Resource Token Creation Request
 */
export interface ResourceTokenCreateRequest {
  resource_id: string;
  description?: string | null;
  quota_events_per_hour?: number;
}

export interface ResourceTokenUpdateRequest {
  description?: string | null;
  quota_events_per_hour?: number;
}

/**
 * Resource Token Creation Response (includes plaintext token ONCE)
 */
export interface ResourceTokenCreateResponse extends ResourceToken {
  token: string; // Plaintext token (ONLY shown once)
}

/**
 * Paginated response for resource tokens
 * Issue #264: Pagination support
 */
export interface PaginatedResourceTokens {
  tokens: ResourceToken[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * Get list of all resource tokens with pagination
 * Issue #264: Added pagination support
 *
 * @param resourceId - Optional filter by resource_id
 * @param limit - Number of tokens per page (default: 50)
 * @param offset - Starting offset (default: 0)
 */
export async function listResourceTokens(
  resourceId?: string,
  limit: number = 50,
  offset: number = 0
): Promise<PaginatedResourceTokens> {
  const searchParams = new URLSearchParams();
  if (resourceId) {
    searchParams.set('resource_id', resourceId);
  }
  searchParams.set('limit', limit.toString());
  searchParams.set('offset', offset.toString());

  return apiClient.get<PaginatedResourceTokens>(`/api/v1/resource-tokens?${searchParams.toString()}`);
}

/**
 * Create a new resource token (Owner only)
 *
 * IMPORTANT: The plaintext token is ONLY returned once.
 * The client MUST save it immediately - it cannot be retrieved again.
 *
 * @param data - Token creation request
 */
export async function createResourceToken(
  data: ResourceTokenCreateRequest
): Promise<ResourceTokenCreateResponse> {
  return apiClient.post<ResourceTokenCreateResponse>(
    '/api/v1/resource-tokens',
    data
  );
}

/**
 * Revoke a resource token (Owner only)
 *
 * Soft delete - token remains in database for audit trail but cannot be used.
 *
 * @param tokenId - Database ID of the token
 */
export async function updateResourceToken(
  tokenId: number,
  data: ResourceTokenUpdateRequest
): Promise<ResourceToken> {
  return apiClient.patch<ResourceToken>(`/api/v1/resource-tokens/${tokenId}`, data);
}

export async function revokeResourceToken(tokenId: number): Promise<void> {
  return apiClient.delete<void>(`/api/v1/resource-tokens/${tokenId}`);
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
 * Helper: Get badge color for status
 */
export function getStatusColor(status: 'active' | 'revoked'): string {
  switch (status) {
    case 'active':
      return 'green';
    case 'revoked':
      return 'gray';
    default:
      return 'gray';
  }
}
