import { describe, expect, it } from "vitest";

import nextConfig from "../../../next.config";
import { metadata } from "./layout";

/**
 * #1588: /join/{token} carries a credential in its path. Both layers — the
 * response headers and the document <meta> — must keep it out of `Referer`
 * and out of search indexes, and must not widen to other routes.
 */
describe("/join referrer + indexing policy (#1588)", () => {
  it("serves /join/* with no-referrer and noindex response headers", async () => {
    const rules = await nextConfig.headers?.();

    expect(rules).toContainEqual({
      source: "/join/:path*",
      headers: [
        { key: "Referrer-Policy", value: "no-referrer" },
        { key: "X-Robots-Tag", value: "noindex, nofollow" },
      ],
    });
  });

  it("scopes the header rules to the token-bearing routes only", async () => {
    const rules = (await nextConfig.headers?.()) ?? [];

    // #1678 adds /password (reset / set-up links carry a token too), #1734
    // /signup (the email sign-up link), #2786 /link (the provider link
    // confirmation), #2761 /email (the announcement unsubscribe link).
    expect(rules.map((rule) => rule.source)).toEqual([
      "/signup/:path*",
      "/link/:path*",
      "/join/:path*",
      "/password/:path*",
      "/email/:path*",
    ]);
  });

  it("repeats both as document metadata for proxies that overwrite headers", () => {
    expect(metadata.referrer).toBe("no-referrer");
    expect(metadata.robots).toEqual({ index: false, follow: false });
  });
});
