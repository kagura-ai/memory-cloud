import { describe, expect, it } from "vitest";

import nextConfig from "../../../next.config";
import { metadata } from "./layout";

/**
 * #2761: /email/unsubscribe#<token> carries the announcement unsubscribe token.
 * Keep the page's URL out of `Referer` and out of search indexes, at the
 * response-header and the document level (same policy as /join and /password).
 */
describe("/email referrer + indexing policy (#2761)", () => {
  it("serves /email/* with no-referrer and noindex response headers", async () => {
    const rules = await nextConfig.headers?.();

    expect(rules).toContainEqual({
      source: "/email/:path*",
      headers: [
        { key: "Referrer-Policy", value: "no-referrer" },
        { key: "X-Robots-Tag", value: "noindex, nofollow" },
      ],
    });
  });

  it("repeats both as document metadata", () => {
    expect(metadata.referrer).toBe("no-referrer");
    expect(metadata.robots).toEqual({ index: false, follow: false });
  });
});
