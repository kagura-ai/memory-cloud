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
 * @returns The validated value, or `undefined` if unsafe.
 */
export function safeReturnTo(
  value: string | null | undefined,
  currentOrigin: string,
): string | undefined {
  if (!value) return undefined;

  // (a) Relative path: must start with exactly one `/`, not `//`.
  // A path like `/redirect?to=https://evil.com` is safe — the browser treats
  // the whole string as a same-site path; what the backend does with inner
  // query params is a separate concern.
  if (value.startsWith("/")) {
    if (value.startsWith("//")) return undefined; // protocol-relative
    return value;
  }

  // (b) Absolute URL: must be same-origin and http(s)
  try {
    const parsed = new URL(value, currentOrigin);
    if (
      parsed.origin === currentOrigin &&
      (parsed.protocol === "http:" || parsed.protocol === "https:")
    ) {
      return value;
    }
    return undefined;
  } catch {
    return undefined;
  }
}
