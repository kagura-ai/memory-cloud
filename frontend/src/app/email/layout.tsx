import type { Metadata } from "next";

/**
 * Layout for the email-link pages (#2761).
 *
 * `/email/unsubscribe#<token>` carries the announcement unsubscribe token in
 * the URL fragment (never sent to a server). The page still keeps its URL out
 * of `Referer` and out of search indexes, like `/join` and `/password`.
 * `next.config.ts` sets the same two values as response headers; these
 * <meta> tags are the backstop for proxies that overwrite them.
 */
export const metadata: Metadata = {
  referrer: "no-referrer",
  robots: { index: false, follow: false },
};

export default function EmailLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
