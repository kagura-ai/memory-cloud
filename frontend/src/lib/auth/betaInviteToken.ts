/**
 * Closed-beta invite tokens on the client (#1655).
 *
 * The pattern mirrors the backend's `BETA_INVITE_TOKEN_PATTERN`
 * (backend/src/services/beta_invite_service.py). A value that does not match
 * could never be redeemed, so the /login invite entry refuses it before
 * navigating anywhere.
 *
 * The token is a credential: callers never log or persist it.
 */

export const BETA_INVITE_TOKEN_PATTERN = /^[A-Za-z0-9_-]{20,128}$/;

export function isBetaInviteToken(value: string): boolean {
  return BETA_INVITE_TOKEN_PATTERN.test(value);
}

// Only used to parse a relative `/join/<token>` paste; never navigated to.
const PARSE_BASE = "http://invite-link.invalid";

/**
 * The token in what someone pasted into the invite entry: a bare token, or a
 * `/join/<token>` link (absolute http(s) or relative). Anything else — another
 * path, a token in a query string, a malformed token — is `null`.
 *
 * Only the path segment is read. The link's own query (including any
 * `return_to`) is ignored: the destination comes from the page's validated
 * `return_to`, never from pasted text.
 */
export function parseBetaInviteInput(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (isBetaInviteToken(trimmed)) return trimmed;

  let url: URL;
  try {
    url = new URL(trimmed, PARSE_BASE);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  const match = /^\/join\/([^/]+)\/?$/.exec(url.pathname);
  if (!match) return null;
  return isBetaInviteToken(match[1]) ? match[1] : null;
}
