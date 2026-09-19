import type { Metadata } from "next";

/**
 * Layout for the beta invite landing page (#1582, #1588).
 *
 * `/join/{token}` carries a one-time invite token in its path. The page itself
 * is a client component, so the document-level policy lives here:
 *
 *  - `referrer: "no-referrer"` — the browser sends no `Referer` from this page
 *    (preview fetch, OAuth navigation, asset requests), so the token is not
 *    copied into a request header that proxies log.
 *  - `robots` — an invite URL must never be indexed; this overrides the root
 *    layout's `index, follow`.
 *
 * `next.config.ts` sets the same two values as response headers. The <meta>
 * tags here are the backstop for deployments whose proxy overwrites them.
 */
export const metadata: Metadata = {
  referrer: "no-referrer",
  robots: { index: false, follow: false },
};

export default function JoinLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
