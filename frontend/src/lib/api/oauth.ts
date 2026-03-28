/**
 * OAuth2 Client and Provider Management API Client
 *
 * Issue #684 - OAuth2 Settings UI
 */

import { apiClient } from './base';

// ============================================================================
// Types
// ============================================================================

export interface OAuth2Provider {
  name: string;
  display_name: string;
  client_id: string | null;
  authorization_url: string;
  token_url: string;
  scopes: string[];
  enabled: boolean;
  configured: boolean;
}

export interface OAuth2Client {
  id: number;
  client_id: string;
  client_name: string;
  redirect_uris: string[];
  grant_types: string[];
  response_types: string[];
  scope: string;
  token_endpoint_auth_method: string;
  provider: string;  // Migration 036: claude, chatgpt, custom
  created_at: string;
  // Migration 034-035: Zero-knowledge visibility
  plaintext_secret: string | null;  // Only if visible + owner
  is_visible: boolean;
  visibility_expires_at: string | null;
}

export interface OAuth2ClientWithSecret extends OAuth2Client {
  client_secret: string;
}

export interface OAuth2ClientCreateRequest {
  client_name: string;
  redirect_uris: string[];
  provider?: string;  // Migration 036: claude, chatgpt, custom (default: custom)
  grant_types?: string[];
  response_types?: string[];
  scope?: string;
  token_endpoint_auth_method?: string;
}

export interface OAuth2ClientUpdateRequest {
  client_name?: string;
  redirect_uris?: string[];
  scope?: string;
}

// ============================================================================
// OAuth2 Provider Management
// ============================================================================

/**
 * List configured OAuth2 providers
 */
export async function getOAuth2Providers(): Promise<OAuth2Provider[]> {
  return await apiClient.get<OAuth2Provider[]>('/api/v1/oauth/providers');
}

// ============================================================================
// OAuth2 Client Management
// ============================================================================

/**
 * List all registered OAuth2 clients
 */
export async function getOAuth2Clients(): Promise<OAuth2Client[]> {
  return await apiClient.get<OAuth2Client[]>('/api/v1/oauth/clients');
}

/**
 * Create a new OAuth2 client
 */
export async function createOAuth2Client(
  data: OAuth2ClientCreateRequest
): Promise<OAuth2ClientWithSecret> {
  return await apiClient.post<OAuth2ClientWithSecret>(
    '/api/v1/oauth/clients',
    data
  );
}

/**
 * Get OAuth2 client details
 */
export async function getOAuth2Client(clientId: string): Promise<OAuth2Client> {
  return await apiClient.get<OAuth2Client>(`/api/v1/oauth/clients/${clientId}`);
}

/**
 * Update OAuth2 client
 */
export async function updateOAuth2Client(
  clientId: string,
  data: OAuth2ClientUpdateRequest
): Promise<OAuth2Client> {
  return await apiClient.put<OAuth2Client>(
    `/api/v1/oauth/clients/${clientId}`,
    data
  );
}

/**
 * Regenerate OAuth2 client secret
 */
export async function regenerateOAuth2ClientSecret(
  clientId: string
): Promise<OAuth2ClientWithSecret> {
  return await apiClient.post<OAuth2ClientWithSecret>(
    `/api/v1/oauth/clients/${clientId}/regenerate-secret`
  );
}

/**
 * Delete OAuth2 client
 */
export async function deleteOAuth2Client(clientId: string): Promise<void> {
  await apiClient.delete(`/api/v1/oauth/clients/${clientId}`);
}
