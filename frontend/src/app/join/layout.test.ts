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

    expect(rules).toEqual([
      {
        source: "/join/:path*",
        headers: [
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Robots-Tag", value: "noindex, nofollow" },
        ],
      },
    ]);
  });

  it("scopes the header rule to /join only", async () => {
    const rules = (await nextConfig.headers?.()) ?? [];

    expect(rules.map((rule) => rule.source)).toEqual(["/join/:path*"]);
  });

  it("repeats both as document metadata for proxies that overwrite headers", () => {
    expect(metadata.referrer).toBe("no-referrer");
    expect(metadata.robots).toEqual({ index: false, follow: false });
  });
});
