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
 *     `/api/v1/api/v1/auth/...`. Strip a trailing `/api/v1` followed by any
 *     number of slashes (so `/api/v1`, `/api/v1/`, and `/api/v1///` all
 *     normalize to no suffix) before appending.
 *
 *  3. **Trailing-slash collapse**: independently of trap 2, the env var may
 *     end with one or more trailing slashes (e.g. `https://api.example.com/`
 *     or `///`). Collapsing them keeps the concatenated path well-formed.
 *
 * `returnTo` is validated: after absolute-ization, it must have an http(s)
 * scheme and the same origin as the current document. Cross-origin URLs and
 * non-http(s) schemes (`javascript:`, `data:`, …) throw `TypeError`. This
 * matters because the backend redirects to `return_to` verbatim on some
 * callback paths (auth.py:607, 1016), so an unvalidated value flowing
 * through here is a CWE-601 open-redirect surface. The validation is the
 * same shape as safeReturnTo (#772) but inlined so the helper is safe by
 * default — callers can pass either a relative same-origin path
 * (`/invite/abc?x=1`) or an already-validated absolute URL.
 *
 * `options.invite` (#1582) carries a beta invite token into the OAuth login
 * (`&invite=<encoded>`), where the backend binds it to the OAuth state. It is
 * appended only for a non-empty string, so the two-argument form is
 * byte-identical to before. The token is a credential — never log the result.
 */
export type OAuthProvider = "google" | "github";

export interface BuildOAuthRedirectOptions {
  /** Beta invite token from `/join/{token}` (#1582). */
  invite?: string;
}

export function buildOAuthRedirect(
  provider: OAuthProvider,
  returnTo: string,
  options?: BuildOAuthRedirectOptions,
): string {
  const absoluteReturnTo = new URL(returnTo, window.location.origin);
  if (
    absoluteReturnTo.origin !== window.location.origin ||
    (absoluteReturnTo.protocol !== "http:" &&
      absoluteReturnTo.protocol !== "https:")
  ) {
    throw new TypeError(
      `buildOAuthRedirect: returnTo must resolve to a same-origin http(s) URL (got ${absoluteReturnTo.protocol}//${absoluteReturnTo.host || "(opaque)"})`,
    );
  }
  const apiBaseUrl = (
    process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"
  )
    .replace(/\/api\/v1\/*$/, "")
    .replace(/\/+$/, "");
  const inviteParam =
    typeof options?.invite === "string" && options.invite !== ""
      ? `&invite=${encodeURIComponent(options.invite)}`
      : "";
  return `${apiBaseUrl}/api/v1/auth/${provider}/login?return_to=${encodeURIComponent(absoluteReturnTo.toString())}${inviteParam}`;
}
