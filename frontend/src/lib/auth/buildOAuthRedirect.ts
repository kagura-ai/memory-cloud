/**
 * Build a direct-browser-navigation URL to the backend OAuth login endpoint.
 *
 * Three non-obvious traps the helper guards against:
 *
 *  1. **Cross-origin redirect resolve**: backend uses `RedirectResponse(url=
 *     return_to_url)` verbatim in some callback paths (auth.py:607, 1016), so
 *     a relative `return_to` would resolve against the API origin in
 *     multi-origin deployments (e.g. `NEXT_PUBLIC_API_URL=https://api.example
 *     .com` vs frontend on `https://app.example.com`). Send an absolute
 *     same-origin URL instead — `new URL(returnTo, window.location.origin)`.
 *
 *  2. **`/api/v1` suffix duplication**: some deployments set
 *     `NEXT_PUBLIC_API_URL=https://api.example.com/api/v1` with the version
 *     suffix baked in. Naively appending `/api/v1/auth/...` would produce
 *     `/api/v1/api/v1/auth/...`. Strip a trailing `/api/v1` (with or without
 *     trailing slash) before appending.
 *
 *  3. **Trailing-slash collapse**: independently of trap 2, the env var may
 *     end with one or more trailing slashes (e.g. `https://api.example.com/`
 *     or `///`). Collapsing them keeps the concatenated path well-formed.
 *
 * Caller must ensure `returnTo` is same-origin-safe — either by passing a
 * value constructed from same-origin parts (e.g. `window.location.pathname +
 * search`) or by pre-validating an external value via safeReturnTo (#772).
 * This helper trusts the input and does not re-validate.
 */
export type OAuthProvider = "google" | "github";

export function buildOAuthRedirect(
  provider: OAuthProvider,
  returnTo: string,
): string {
  const absoluteReturnTo = new URL(returnTo, window.location.origin).toString();
  const apiBaseUrl = (
    process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"
  )
    .replace(/\/api\/v1\/?$/, "")
    .replace(/\/+$/, "");
  return `${apiBaseUrl}/api/v1/auth/${provider}/login?return_to=${encodeURIComponent(absoluteReturnTo)}`;
}
