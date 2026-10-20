/**
 * Authorized OAuth apps ("connected apps") of the signed-in user (#2129).
 *
 * Session-cookie only. Each grant can carry an MCP default context: a
 * context-scoped tool called over a bearer token of that app without
 * `context_id` runs in it (`?context=` on the URL wins).
 */

import { apiClient } from "./base";
import type { OAuthGrant } from "../types/oauth-grant";

/** List the OAuth apps the user has authorized, newest issuance first. */
export async function listOAuthGrants(): Promise<OAuthGrant[]> {
  return apiClient.get<OAuthGrant[]>("/api/v1/me/oauth/grants");
}

function defaultContextPath(clientId: string): string {
  return `/api/v1/me/oauth/grants/${encodeURIComponent(clientId)}/default-context`;
}

/** Set one grant's MCP default context (404 when the context is unreadable). */
export async function setOAuthGrantDefaultContext(
  clientId: string,
  contextId: string,
): Promise<OAuthGrant> {
  return apiClient.put<OAuthGrant>(defaultContextPath(clientId), {
    default_context_id: contextId,
  });
}

/** Clear one grant's MCP default context. */
export async function clearOAuthGrantDefaultContext(
  clientId: string,
): Promise<void> {
  return apiClient.delete<void>(defaultContextPath(clientId));
}
