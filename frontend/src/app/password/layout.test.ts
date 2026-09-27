import { describe, expect, it } from "vitest";

import nextConfig from "../../../next.config";
import { metadata } from "./layout";

/**
 * #1678: /password/reset and /password/setup carry a one-time token in the
 * query string. Keep it out of `Referer` and out of search indexes, at the
 * response-header and the document level (same policy as /join, #1588).
 */
describe("/password referrer + indexing policy (#1678)", () => {
  it("serves /password/* with no-referrer and noindex response headers", async () => {
    const rules = await nextConfig.headers?.();

    expect(rules).toContainEqual({
      source: "/password/:path*",
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
