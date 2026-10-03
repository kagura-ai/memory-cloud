/**
 * lib/mcp/url — the single MCP URL derivation (#1836).
 *
 * withCoreProfile / toBareMcpUrl / the command builders are exercised through
 * MCPConfigBlock.test.tsx (re-exported there); this file pins the parts that
 * module did not cover: the API-origin strip and the bare / pinned pair.
 */
import { describe, expect, it } from "vitest";

import { apiOrigin, mcpEndpoints } from "./url";

describe("apiOrigin", () => {
  it.each([
    ["https://host/api/v1", "https://host"],
    ["https://host/api/v1/", "https://host"],
    ["https://host", "https://host"],
    ["https://host/", "https://host"],
    ["http://localhost:8080/api/v1", "http://localhost:8080"],
  ])("%s → %s", (input, expected) => {
    expect(apiOrigin(input)).toBe(expected);
  });
});

describe("mcpEndpoints", () => {
  it("derives the bare endpoint and the workspace-pinned form from one origin", () => {
    expect(mcpEndpoints("https://host/api/v1/", "ws-1")).toEqual({
      baseUrl: "https://host",
      mcpUrl: "https://host/mcp",
      pinnedUrl: "https://host/mcp/w/ws-1",
    });
  });

  it("has no pinned form without a workspace", () => {
    expect(mcpEndpoints("https://host/api/v1", null).pinnedUrl).toBeNull();
  });
});
