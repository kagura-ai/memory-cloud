/**
 * lib/mcp/url — the single MCP URL derivation (#1836).
 *
 * withProfile / toBareMcpUrl / the command builders are exercised through
 * MCPConfigBlock.test.tsx (re-exported there); this file pins the parts that
 * module did not cover: the API-origin strip and the bare / pinned pair.
 */
import { describe, expect, it } from "vitest";

import { apiOrigin, mcpEndpoints, withAdditionalTools } from "./url";

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

describe("withAdditionalTools", () => {
  it("normalizes and deduplicates comma-separated tool names", () => {
    expect(withAdditionalTools("https://host/mcp", " explore, setup_resource, explore "))
      .toBe("https://host/mcp?add_tools=explore,setup_resource");
  });
  it("replaces or clears additional tools without losing query options or a fragment", () => {
    expect(withAdditionalTools("https://host/mcp?context=demo&add_tools=old#help", "explore"))
      .toBe("https://host/mcp?context=demo&add_tools=explore#help");
    expect(withAdditionalTools("https://host/mcp?add_tools=old#help", ""))
      .toBe("https://host/mcp#help");
  });
  it("encodes names so typed text cannot inject query parameters", () => {
    expect(withAdditionalTools("https://host/mcp", "explore&tools=other"))
      .toBe("https://host/mcp?add_tools=explore%26tools%3Dother");
  });
});
