import type { Metadata } from "next";

/**
 * Layout for the provider link confirmation page (#2786).
 *
 * `/link/confirm?token=…` carries a one-time link token in the URL. Like
 * `/join` (#1588), `/password` (#1678) and `/signup` (#1734), the document
 * keeps it out of `Referer` and out of search indexes. `next.config.ts` sets
 * the same two values as response headers; these <meta> tags are the
 * backstop for proxies that overwrite them.
 */
export const metadata: Metadata = {
  referrer: "no-referrer",
  robots: { index: false, follow: false },
};

export default function LinkLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
