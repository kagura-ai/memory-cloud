import { describe, expect, it } from "vitest";

import nextConfig from "../../../next.config";
import { metadata } from "./layout";

/**
 * #2786: /link/confirm carries a one-time link token in the query string.
 * Keep it out of `Referer` and out of search indexes, at the response-header
 * and the document level (same policy as /join, #1588).
 */
describe("/link referrer + indexing policy (#2786)", () => {
  it("serves /link/* with no-referrer and noindex response headers", async () => {
    const rules = await nextConfig.headers?.();

    expect(rules).toContainEqual({
      source: "/link/:path*",
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
