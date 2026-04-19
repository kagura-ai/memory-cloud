import { apiClient } from "./base";

export interface ExternalAPIKey {
  id: number;
  key_name: string;
  provider: string;
  masked_value: string;
  enabled: boolean; // Issue #105
  created_at: string;
  updated_at: string;
  updated_by: string | null;
}

export interface CreateExternalAPIKeyRequest {
  key_name: string;
  provider: string;
  value: string;
  enabled?: boolean; // Issue #105, defaults to true
}

export interface UpdateExternalAPIKeyRequest {
  value: string;
}

export interface ToggleExternalAPIKeyRequest {
  enabled: boolean;
}

export interface ExternalKeyListResponse {
  keys: ExternalAPIKey[];
  total: number;
}

/**
 * List all external API keys, optionally filtered by provider
 */
export async function listExternalAPIKeys(
  provider?: string,
): Promise<ExternalAPIKey[]> {
  const params = provider ? `?provider=${provider}` : "";
  const response = await apiClient.get<ExternalKeyListResponse>(
    `/api/v1/external-keys${params}`,
  );
  return response.keys;
}

/**
 * Create a new external API key
 */
export async function createExternalAPIKey(
  data: CreateExternalAPIKeyRequest,
): Promise<ExternalAPIKey> {
  return apiClient.post<ExternalAPIKey>("/api/v1/external-keys", data);
}

/**
 * Update an existing external API key
 */
export async function updateExternalAPIKey(
  keyName: string,
  value: string,
): Promise<ExternalAPIKey> {
  return apiClient.put<ExternalAPIKey>(`/api/v1/external-keys/${keyName}`, {
    value,
  });
}

/**
 * Toggle enabled/disabled state (Issue #105)
 */
export async function toggleExternalAPIKey(
  keyName: string,
  enabled: boolean,
): Promise<ExternalAPIKey> {
  return apiClient.patch<ExternalAPIKey>(
    `/api/v1/external-keys/${keyName}/toggle`,
    { enabled },
  );
}

/**
 * Delete an external API key
 */
export async function deleteExternalAPIKey(keyName: string): Promise<void> {
  await apiClient.delete(`/api/v1/external-keys/${keyName}`);
}
