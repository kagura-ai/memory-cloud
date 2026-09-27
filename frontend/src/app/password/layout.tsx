import type { Metadata } from "next";

/**
 * Layout for the password recovery pages (#1678).
 *
 * `/password/reset?token=…` and `/password/setup?token=…` carry a one-time
 * token in the URL. Like `/join` (#1588), the document keeps it out of
 * `Referer` and out of search indexes. `next.config.ts` sets the same two
 * values as response headers; these <meta> tags are the backstop for proxies
 * that overwrite them.
 */
export const metadata: Metadata = {
  referrer: "no-referrer",
  robots: { index: false, follow: false },
};

export default function PasswordLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
