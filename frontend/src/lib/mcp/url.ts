/**
 * MCP endpoint URLs and the client one-liners built from them.
 *
 * One module for every place that derives a URL from `NEXT_PUBLIC_API_URL`
 * (McpConnectionCard, APIKeysTabPanel, MCPConfigBlock), so the `/api/v1`
 * strip, the bare / workspace-pinned forms and the `?profile=core` query are
 * written once (#1836). Pure functions; no React.
 */

/** The environment variable Codex reads the API key from (#1624). */
export const CODEX_BEARER_TOKEN_ENV_VAR = "KAGURA_API_KEY";

/** The query the server reads to list the 12 core tools only (#1601). */
const CORE_PROFILE_QUERY = "profile=core";

/**
 * The server origin behind an API base URL: `NEXT_PUBLIC_API_URL` may carry
 * the `/api/v1` REST suffix (with or without a trailing slash); both are
 * stripped, as `lib/auth/buildOAuthRedirect.ts` does.
 */
export function apiOrigin(apiUrl: string): string {
  return apiUrl.replace(/\/api\/v1\/*$/, "").replace(/\/+$/, "");
}

/**
 * The MCP endpoints for an API base URL: the bare `…/mcp` every client can
 * use (an API key carries its workspace; OAuth resolves it at login), and the
 * workspace-pinned `…/mcp/w/<id>` (null without a workspace) that keeps an
 * OAuth connector on one workspace.
 */
export function mcpEndpoints(
  apiUrl: string,
  workspaceId: string | null,
): { baseUrl: string; mcpUrl: string; pinnedUrl: string | null } {
  const baseUrl = apiOrigin(apiUrl);
  return {
    baseUrl,
    mcpUrl: `${baseUrl}/mcp`,
    pinnedUrl: workspaceId ? `${baseUrl}/mcp/w/${workspaceId}` : null,
  };
}

/**
 * Strip the workspace-pinned `/w/<workspaceId>` suffix from an MCP URL,
 * yielding the bare `…/mcp` endpoint (#988). Idempotent.
 */
export function toBareMcpUrl(mcpUrl: string): string {
  return mcpUrl.replace(/\/w\/[^/]+$/, "");
}

/**
 * Put `profile=core` on an MCP endpoint URL (#1609).
 *
 * Plain string handling rather than `new URL()`: the snippet must show the
 * URL exactly as issued (no normalization), and a placeholder URL must not
 * throw. An existing query is kept and joined with `&`; an existing `profile`
 * parameter is replaced rather than repeated, which also makes the helper
 * idempotent; a fragment stays last.
 */
export function withCoreProfile(mcpUrl: string): string {
  const hashAt = mcpUrl.indexOf("#");
  const beforeHash = hashAt === -1 ? mcpUrl : mcpUrl.slice(0, hashAt);
  const hash = hashAt === -1 ? "" : mcpUrl.slice(hashAt);
  const queryAt = beforeHash.indexOf("?");
  if (queryAt === -1) return `${beforeHash}?${CORE_PROFILE_QUERY}${hash}`;
  const params = beforeHash
    .slice(queryAt + 1)
    .split("&")
    .filter((p) => p !== "" && p !== "profile" && !p.startsWith("profile="));
  const query = [...params, CORE_PROFILE_QUERY].join("&");
  return `${beforeHash.slice(0, queryAt)}?${query}${hash}`;
}

/**
 * Render a URL as one shell argument. `?` is a glob character (zsh aborts the
 * command with "no matches found") and `&` ends the command, so a URL with a
 * query is wrapped in double quotes — understood by sh, zsh, PowerShell and
 * cmd alike. A URL without one is left bare.
 */
export function shellUrlArg(url: string): string {
  return /[?&]/.test(url) ? `"${url}"` : url;
}

/**
 * The Claude Code OAuth one-liner (#988):
 * `claude mcp add --transport http kagura-memory <url>`. Needs no API key —
 * Claude Code runs the OAuth browser flow on first use.
 */
export function buildClaudeOAuthCommand(mcpUrl: string): string {
  return `claude mcp add --transport http kagura-memory ${shellUrlArg(mcpUrl)}`;
}

/**
 * The Codex CLI one-liner that registers the server (#1624):
 * `codex mcp add kagura-memory --url <url> --bearer-token-env-var KAGURA_API_KEY`.
 * Env-aware via the URL, quotes the URL when it carries a query, and never
 * carries the key.
 */
export function buildCodexAddCommand(mcpUrl: string): string {
  return `codex mcp add kagura-memory --url ${shellUrlArg(mcpUrl)} --bearer-token-env-var ${CODEX_BEARER_TOKEN_ENV_VAR}`;
}
