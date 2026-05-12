/**
 * Member Credentials API Client
 *
 * Migration 034: Member-scoped API Keys and OAuth Apps
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

export interface MemberAPIKey {
  id: number;
  name: string;
  key_prefix: string;
  plaintext_key: string | null; // Only if visible + owner
  is_visible: boolean;
  visibility_expires_at: string | null;
  created_at: string;
  revoked_at: string | null;
  // Issue #626: Public-context attribution. When non-null, this key is a
  // public-bound key — attributed to one is_public=true context on the
  // public REST endpoint (per-key rate-limit + audit + independent revoke).
  // Binding is immutable; revoke and re-create to change.
  bound_context_id: string | null;
}

export interface MemberOAuthApp {
  client_id: string;
  client_name: string;
  plaintext_secret: string | null; // Only if visible + owner
  is_visible: boolean;
  visibility_expires_at: string | null;
  created_at: string;
  redirect_uris: string[];
  scope: string;
}

export interface MemberCredentials {
  api_keys: MemberAPIKey[]; // Multiple API keys support
  target_user_role: string; // Target user's workspace role (for permission checks)
}

/**
 * Get or create member credentials (Lazy initialization)
 */
export async function getMemberCredentials(
  workspaceId: string,
  userId: string,
): Promise<MemberCredentials> {
  const response = await fetch(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/members/${userId}/credentials`,
    {
      credentials: "include",
    },
  );

  if (!response.ok) {
    throw new Error(`Failed to get credentials: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Manually hide API key (Owner only)
 */
export async function hideAPIKey(
  workspaceId: string,
  userId: string,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/members/${userId}/credentials/api-key/hide`,
    {
      method: "POST",
      credentials: "include",
    },
  );

  if (!response.ok) {
    throw new Error(`Failed to hide API key: ${response.statusText}`);
  }
}

/**
 * Regenerate API key (Owner only)
 */
export async function regenerateAPIKey(
  workspaceId: string,
  userId: string,
): Promise<{ key: string; key_prefix: string; key_id: number }> {
  const response = await fetch(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/members/${userId}/credentials/api-key/regenerate`,
    {
      method: "POST",
      credentials: "include",
    },
  );

  if (!response.ok) {
    throw new Error(`Failed to regenerate API key: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Create new API key (Owner only).
 *
 * Issue #626: When `bound_context_id` is supplied, the key is created as
 * a public-bound key — attributed to one `is_public=true` context on the
 * public REST endpoint. The binding is immutable; revoke and re-create to
 * change.
 */
export async function createAPIKey(
  workspaceId: string,
  userId: string,
  data: {
    name: string;
    auto_hide_minutes?: number;
    bound_context_id?: string;
  },
): Promise<MemberAPIKey> {
  const response = await fetch(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/members/${userId}/credentials/api-keys`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      credentials: "include",
      body: JSON.stringify(data),
    },
  );

  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new Error(
      error.detail || `Failed to create API key: ${response.statusText}`,
    );
  }

  return response.json();
}

/**
 * Delete a specific API key by id (Owner only).
 *
 * Issue #626: Required for revoking public-bound keys, which have
 * `workspace_id IS NULL` and so are invisible to the legacy singleton
 * `deleteWorkspaceMemberAPIKey` endpoint. This per-id endpoint accepts
 * both regular workspace-scoped keys and public-bound keys belonging to
 * the user.
 */
export async function deleteWorkspaceMemberAPIKeyById(
  workspaceId: string,
  userId: string,
  keyId: number,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/members/${userId}/credentials/api-keys/${keyId}`,
    {
      method: "DELETE",
      credentials: "include",
    },
  );

  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new Error(
      error.detail || `Failed to delete API key: ${response.statusText}`,
    );
  }
}

/**
 * Delete member API key (Permission-based)
 *
 * @deprecated Backend limitation: This function currently deletes the MOST RECENT API key
 * for the specified user, NOT the key identified by keyId parameter.
 *
 * The keyId parameter is accepted for API compatibility but is currently ignored by the backend.
 * Use with extreme caution - verify which key will be deleted before calling.
 *
 * @param workspaceId - Workspace ID
 * @param userId - Target user ID
 * @param keyId - API key ID (CURRENTLY IGNORED by backend)
 *
 * @todo Once backend implements DELETE /api/v1/workspaces/{workspaceId}/members/{userId}/credentials/api-keys/{keyId}
 * remove the @deprecated tag and update the endpoint URL.
 *
 * @see Issue #XXX - Multiple API Key deletion support
 */
export async function deleteWorkspaceMemberAPIKey(
  workspaceId: string,
  userId: string,
  keyId?: number, // CURRENTLY IGNORED - Backend deletes most recent key
): Promise<void> {
  // WARNING: Backend limitation - keyId is ignored, most recent key is deleted
  // TODO: Update to /credentials/api-keys/${keyId} when backend supports it
  const response = await fetch(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/members/${userId}/credentials/api-key`,
    {
      method: "DELETE",
      credentials: "include",
    },
  );

  if (!response.ok) {
    throw new Error(`Failed to delete API key: ${response.statusText}`);
  }

  // TODO: Log warning in development if keyId is provided but ignored
  if (process.env.NODE_ENV === "development" && keyId !== undefined) {
    console.warn(
      `deleteWorkspaceMemberAPIKey: keyId=${keyId} provided but ignored. Backend deletes most recent key.`,
    );
  }
}

/**
 * Manually hide OAuth app secret (Owner only)
 * Note: Use OAuth clients API directly
 */
export async function hideOAuthClientSecret(clientId: string): Promise<void> {
  const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

  const response = await fetch(
    `${API_BASE}/api/v1/oauth/clients/${clientId}/hide`,
    {
      method: "POST",
      credentials: "include",
    },
  );

  if (!response.ok) {
    throw new Error(`Failed to hide OAuth app: ${response.statusText}`);
  }
}

/**
 * Regenerate OAuth client secret (Owner only)
 */
export async function regenerateOAuthSecret(
  workspaceId: string,
  userId: string,
): Promise<{ client_secret: string; client_id: string }> {
  const response = await fetch(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/members/${userId}/credentials/oauth/regenerate`,
    {
      method: "POST",
      credentials: "include",
    },
  );

  if (!response.ok) {
    throw new Error(
      `Failed to regenerate OAuth secret: ${response.statusText}`,
    );
  }

  return response.json();
}

/**
 * Delete OAuth app (Permission-based)
 */
export async function deleteOAuthApp(
  workspaceId: string,
  userId: string,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/members/${userId}/credentials/oauth`,
    {
      method: "DELETE",
      credentials: "include",
    },
  );

  if (!response.ok) {
    throw new Error(`Failed to delete OAuth app: ${response.statusText}`);
  }
}
