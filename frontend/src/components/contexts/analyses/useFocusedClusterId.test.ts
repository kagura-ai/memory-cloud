/**
 * Unit tests for parseClusterParam — the pure half of the hook.
 *
 * The full hook integration (URL round-trip, useEffect strip) is
 * exercised at the page-render level rather than here; following the
 * PR #527 convention this file stays at pure-helper scope.
 */

import { describe, it, expect } from "vitest";
import { parseClusterParam } from "./useFocusedClusterId";

describe("parseClusterParam", () => {
  it("returns null on missing input", () => {
    expect(parseClusterParam(null)).toBeNull();
    expect(parseClusterParam("")).toBeNull();
    expect(parseClusterParam("   ")).toBeNull();
  });

  it("parses non-negative integers", () => {
    expect(parseClusterParam("0")).toBe(0);
    expect(parseClusterParam("3")).toBe(3);
    expect(parseClusterParam("12")).toBe(12);
  });

  it("rejects negatives and non-integers", () => {
    // Critical: a deep-link with garbage like ?cluster=-1 must NOT
    // produce a NaN-driven focus state that crashes the SVG render.
    expect(parseClusterParam("-1")).toBeNull();
    expect(parseClusterParam("1.5")).toBeNull();
    expect(parseClusterParam("abc")).toBeNull();
    expect(parseClusterParam("Infinity")).toBeNull();
    expect(parseClusterParam("NaN")).toBeNull();
  });

  it("trims whitespace before parsing", () => {
    expect(parseClusterParam(" 4 ")).toBe(4);
  });
});
