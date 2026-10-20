/**
 * OAuth grant types (#2129).
 *
 * One OAuth app (client) the signed-in user has authorized, from
 * `GET /api/v1/me/oauth/grants`.
 */

export interface OAuthGrant {
  client_id: string;
  client_name: string;
  scopes: string[];
  // The grant's MCP default context; the name is null when none is set or
  // the user can no longer read the context.
  default_context_id: string | null;
  default_context_name: string | null;
  // When the newest live token was issued; null when none is live.
  last_issued_at: string | null;
}
