/**
 * CWE-601 open-redirect defense for `return_to` query parameters.
 *
 * Accepts only:
 *   (a) Relative paths starting with a single `/` (not `//`, not empty)
 *   (b) Absolute URLs whose origin matches `currentOrigin` and whose
 *       protocol is `http:` or `https:`
 *
 * All other values — including `javascript:`, `data:`, and cross-origin
 * absolute URLs — are rejected and return `undefined`.
 *
 * @param value        The raw `return_to` query-parameter value.
 * @param currentOrigin  `window.location.origin` at the call site (passed
 *                       explicitly so this function is testable without a DOM).
 *                       Pass `""` in SSR; absolute-URL inputs will always be
 *                       rejected in that case.
 * @returns The validated value, or `undefined` if unsafe.
 */
export function safeReturnTo(
  value: string | null | undefined,
  currentOrigin: string,
): string | undefined {
  const trimmed = (value ?? "").trim();
  if (!trimmed) return undefined;

  // (a) Relative path: must start with exactly one `/`, not `//`.
  // A path like `/redirect?to=https://evil.com` is safe — the browser treats
  // the whole string as a same-site path; what the backend does with inner
  // query params is a separate concern.
  if (trimmed.startsWith("/")) {
    if (trimmed.startsWith("//")) return undefined; // protocol-relative
    return trimmed;
  }

  // (b) Absolute URL: must be explicitly http(s), same-origin.
  // Non-/-prefixed relative paths (e.g. "dashboard") are rejected here —
  // they don't start with "/" (branch above) and don't carry an explicit
  // scheme, so they fall through to the undefined return below.
  if (!trimmed.startsWith("http://") && !trimmed.startsWith("https://")) {
    return undefined;
  }
  try {
    const parsed = new URL(trimmed);
    if (
      parsed.origin === currentOrigin &&
      (parsed.protocol === "http:" || parsed.protocol === "https:")
    ) {
      return trimmed;
    }
    return undefined;
  } catch {
    return undefined;
  }
}
