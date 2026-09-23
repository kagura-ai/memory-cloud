/**
 * Where a signed-in visitor is sent on (#1594; shared by /login and
 * /join/[token] since #1655).
 *
 * This is the one place the frontend navigates to `return_to` by itself —
 * everywhere else the value goes to the backend, or through
 * buildOAuthRedirect, and both re-validate it. So the already-sanitized value
 * (see safeReturnTo) is not trusted as a string here: it is resolved the way
 * the router will resolve it, must land on `origin`, and is handed over as a
 * path.
 *
 * The second resolve is not redundant. A same-origin URL can carry a `//host`
 * pathname (`https://app.example//evil.example`), which reads as
 * protocol-relative once reduced to a path.
 *
 * `origin` is `window.location.origin` at the call site; pass `""` during
 * SSR and every value falls back to the default.
 */

export const DEFAULT_FORWARD_TARGET = "/workspace/dashboard";

export function resolveForwardTarget(
  returnTo: string | undefined,
  origin: string,
): string {
  if (!returnTo || !origin) return DEFAULT_FORWARD_TARGET;
  try {
    const url = new URL(returnTo, origin);
    if (url.origin !== origin) return DEFAULT_FORWARD_TARGET;
    const path = `${url.pathname}${url.search}${url.hash}`;
    if (new URL(path, origin).origin !== origin) return DEFAULT_FORWARD_TARGET;
    return path;
  } catch {
    return DEFAULT_FORWARD_TARGET;
  }
}
