/**
 * resolveForwardTarget (#1594, shared since #1655) — the one place the
 * frontend navigates to a `return_to` by itself. Pure: the origin is passed in.
 */
import { describe, expect, it } from "vitest";

import {
  DEFAULT_FORWARD_TARGET,
  resolveForwardTarget,
} from "./resolveForwardTarget";

const ORIGIN = "https://app.example";

describe("resolveForwardTarget", () => {
  it("falls back to the dashboard when there is no return_to", () => {
    expect(DEFAULT_FORWARD_TARGET).toBe("/workspace/dashboard");
    expect(resolveForwardTarget(undefined, ORIGIN)).toBe(DEFAULT_FORWARD_TARGET);
    expect(resolveForwardTarget("", ORIGIN)).toBe(DEFAULT_FORWARD_TARGET);
  });

  it("keeps a relative path with its query and hash", () => {
    expect(resolveForwardTarget("/device?user_code=ABCD1234#x", ORIGIN)).toBe(
      "/device?user_code=ABCD1234#x",
    );
  });

  it("reduces a same-origin absolute URL to a path", () => {
    expect(
      resolveForwardTarget(`${ORIGIN}/api/v1/oauth/authorize?a=1`, ORIGIN),
    ).toBe("/api/v1/oauth/authorize?a=1");
  });

  it("falls back when a same-origin URL carries a //host pathname", () => {
    expect(resolveForwardTarget(`${ORIGIN}//evil.example/x`, ORIGIN)).toBe(
      DEFAULT_FORWARD_TARGET,
    );
  });

  it.each([
    ["a cross-origin URL", "https://evil.example/x"],
    ["a protocol-relative URL", "//evil.example/x"],
    ["a backslash path", "/\\evil.example"],
    ["javascript:", "javascript:alert(1)"],
  ])("falls back for %s", (_label, value) => {
    expect(resolveForwardTarget(value, ORIGIN)).toBe(DEFAULT_FORWARD_TARGET);
  });

  it("falls back when there is no origin to resolve against (SSR)", () => {
    expect(resolveForwardTarget("/device", "")).toBe(DEFAULT_FORWARD_TARGET);
  });
});
