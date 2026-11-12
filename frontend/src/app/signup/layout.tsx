import type { Metadata } from "next";

/**
 * Layout for the email sign-up pages (#1734).
 *
 * `/signup/verify?token=…` carries a one-time account-creation token in the
 * URL. Like `/join` (#1588) and `/password` (#1678), the document keeps it out
 * of `Referer` and out of search indexes. `next.config.ts` sets the same two
 * values as response headers; these <meta> tags are the backstop for proxies
 * that overwrite them.
 */
export const metadata: Metadata = {
  referrer: "no-referrer",
  robots: { index: false, follow: false },
};

export default function SignupLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
