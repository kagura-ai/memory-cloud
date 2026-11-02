/**
 * Member Credentials API Client
 *
 * Migration 034: Member-scoped API Keys and OAuth Apps
 *
 * Every wrapper goes through `apiClient` (#2384), so a refusal reaches the
 * caller as an `ApiError` carrying the error code, the details and the
 * normalised plan-gate facts (`err.gate`) — the UI renders a plan limit on
 * key creation like every other gate refusal. The message keeps the app's
 * precedence: the envelope's `message`, then a plain `detail` string, then
 * the HTTP status.
 */

import { apiClient } from "./base";

/** The member's credentials collection; every wrapper below sits under it. */
function credentialsPath(workspaceId: string, userId: string): string {
  return `/api/v1/workspaces/${workspaceId}/members/${userId}/credentials`;
}

export interface MemberAPIKey {
  id: string; // Public id: "akey_" + 22 base62 chars (#1008)
  name: string;
  key_prefix: string;
  plaintext_key: string | null; // Only if visible + owner
  is_visible: boolean;
  visibility_expires_at: string | null;
  created_at: string;
  last_used_at: string | null; // Issue #943: "Last used" column
  revoked_at: string | null;
  // Issue #626: Public-context attribution. When non-null, this key is a
  // public-bound key — attributed to one is_public=true context on the
  // public REST endpoint (per-key rate-limit + audit + independent revoke).
  // Binding is immutable; revoke and re-create to change.
  bound_context_id: string | null;
  // #2188: the key's MCP default context (#2129). The name is null when
  // unset or when the viewer cannot read that context.
  default_context_id: string | null;
  default_context_name: string | null;
  // #2188: set when the key is bound to an agent — the agent's default
  // binding applies, so the key cannot carry a default context.
  agent_id: string | null;
  // #2299: set when the key is a connector's shared key (#1952). Classify
  // connector keys by this, never by the `connector:` display name.
  connector_id: string | null;
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
  return apiClient.get<MemberCredentials>(
    credentialsPath(workspaceId, userId),
  );
}

/**
 * Manually hide API key (Owner only)
 */
export async function hideAPIKey(
  workspaceId: string,
  userId: string,
): Promise<void> {
  await apiClient.post<void>(
    `${credentialsPath(workspaceId, userId)}/api-key/hide`,
  );
}

/**
 * Regenerate a selected workspace API key (permission-based).
 */
export async function regenerateAPIKey(
  workspaceId: string,
  userId: string,
  keyId: string,
): Promise<{ key: string; key_prefix: string; key_id: string }> {
  return apiClient.post<{ key: string; key_prefix: string; key_id: string }>(
    `${credentialsPath(workspaceId, userId)}/api-key/regenerate?key_id=${encodeURIComponent(keyId)}`,
  );
}

/**
 * Create new API key (Owner only).
 *
 * Issue #626: When `bound_context_id` is supplied, the key is created as
 * a public-bound key — attributed to one `is_public=true` context on the
 * public REST endpoint. The binding is immutable; revoke and re-create to
 * change.
 *
 * #2188: `default_context_id` sets the key's MCP default context in the same
 * call (not with `bound_context_id`). 404 "Context not found" when the caller
 * cannot read it; no key is created then.
 */
export async function createAPIKey(
  workspaceId: string,
  userId: string,
  data: {
    name: string;
    auto_hide_minutes?: number;
    bound_context_id?: string;
    default_context_id?: string;
  },
): Promise<MemberAPIKey> {
  return apiClient.post<MemberAPIKey>(
    `${credentialsPath(workspaceId, userId)}/api-keys`,
    data,
  );
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
  keyId: string,
): Promise<void> {
  await apiClient.delete<void>(
    `${credentialsPath(workspaceId, userId)}/api-keys/${keyId}`,
  );
}

/**
 * Delete member API key by id (Permission-based).
 *
 * Issue #626: rewritten on top of the per-id DELETE endpoint
 * (`/credentials/api-keys/{keyId}`) — the legacy singleton DELETE this
 * function previously wrapped IGNORED its `keyId` arg and deleted the
 * most-recent active key, which was unsafe for callers that wanted a
 * specific key. With the backend now exposing per-id deletion (added
 * in #626), the function is now a thin alias around
 * ``deleteWorkspaceMemberAPIKeyById`` so existing call sites get
 * correct behavior without renaming. ``keyId`` is now required.
 */
export async function deleteWorkspaceMemberAPIKey(
  workspaceId: string,
  userId: string,
  keyId: string,
): Promise<void> {
  return deleteWorkspaceMemberAPIKeyById(workspaceId, userId, keyId);
}

/**
 * Manually hide OAuth app secret (Owner only)
 * Note: Use OAuth clients API directly
 */
export async function hideOAuthClientSecret(clientId: string): Promise<void> {
  await apiClient.post<void>(`/api/v1/oauth/clients/${clientId}/hide`);
}

/**
 * Regenerate OAuth client secret (Owner only)
 */
export async function regenerateOAuthSecret(
  workspaceId: string,
  userId: string,
): Promise<{ client_secret: string; client_id: string }> {
  return apiClient.post<{ client_secret: string; client_id: string }>(
    `${credentialsPath(workspaceId, userId)}/oauth/regenerate`,
  );
}

/**
 * Delete OAuth app (Permission-based)
 */
export async function deleteOAuthApp(
  workspaceId: string,
  userId: string,
): Promise<void> {
  await apiClient.delete<void>(`${credentialsPath(workspaceId, userId)}/oauth`);
}
