/**
 * Unit tests for safeReturnTo — CWE-601 open-redirect defense (#772).
 *
 * safeReturnTo is a pure function with an explicit `currentOrigin` parameter,
 * so no DOM is required.
 */

import { describe, expect, it } from "vitest";
import { safeReturnTo } from "./safeReturnTo";

const ORIGIN = "https://memory.kagura-ai.com";

describe("safeReturnTo", () => {
  // -------------------------------------------------------------------------
  // Accept cases — same-origin relative paths
  // -------------------------------------------------------------------------

  describe("accept — relative paths", () => {
    it("accepts a path with query string", () => {
      expect(safeReturnTo("/device?user_code=ABC123", ORIGIN)).toBe(
        "/device?user_code=ABC123",
      );
    });

    it("accepts root path /", () => {
      expect(safeReturnTo("/", ORIGIN)).toBe("/");
    });

    it("accepts a nested path with query params", () => {
      expect(safeReturnTo("/foo/bar?x=1", ORIGIN)).toBe("/foo/bar?x=1");
    });

    it("trims leading/trailing whitespace from a relative path", () => {
      expect(safeReturnTo("  /dashboard  ", ORIGIN)).toBe("/dashboard");
    });
  });

  // -------------------------------------------------------------------------
  // Accept cases — same-origin absolute URLs
  // -------------------------------------------------------------------------

  describe("accept — same-origin absolute URL", () => {
    it("accepts an https absolute URL whose origin matches", () => {
      expect(safeReturnTo(`${ORIGIN}/device?user_code=ABC`, ORIGIN)).toBe(
        `${ORIGIN}/device?user_code=ABC`,
      );
    });
  });

  // -------------------------------------------------------------------------
  // Reject cases — null / undefined / empty
  // -------------------------------------------------------------------------

  describe("reject — null / undefined / empty inputs", () => {
    it("rejects null", () => {
      expect(safeReturnTo(null, ORIGIN)).toBeUndefined();
    });

    it("rejects undefined", () => {
      expect(safeReturnTo(undefined, ORIGIN)).toBeUndefined();
    });

    it("rejects empty string", () => {
      expect(safeReturnTo("", ORIGIN)).toBeUndefined();
    });

    it("rejects whitespace-only string", () => {
      expect(safeReturnTo("   ", ORIGIN)).toBeUndefined();
    });

    it("rejects tab/newline-only string", () => {
      expect(safeReturnTo("\t\n", ORIGIN)).toBeUndefined();
    });
  });

  // -------------------------------------------------------------------------
  // Reject cases — protocol-relative and cross-origin
  // -------------------------------------------------------------------------

  describe("reject — protocol-relative and cross-origin", () => {
    it("rejects protocol-relative URL //evil.com/x", () => {
      expect(safeReturnTo("//evil.com/x", ORIGIN)).toBeUndefined();
    });

    it("rejects cross-origin https URL", () => {
      expect(safeReturnTo("https://evil.com/x", ORIGIN)).toBeUndefined();
    });

    it("rejects cross-origin http URL", () => {
      expect(safeReturnTo("http://evil.com", ORIGIN)).toBeUndefined();
    });
  });

  // -------------------------------------------------------------------------
  // Reject cases — dangerous schemes
  // -------------------------------------------------------------------------

  describe("reject — dangerous non-http(s) schemes", () => {
    it("rejects javascript: scheme", () => {
      expect(safeReturnTo("javascript:alert(1)", ORIGIN)).toBeUndefined();
    });

    it("rejects data: URI", () => {
      expect(
        safeReturnTo("data:text/html,<script>alert(1)</script>", ORIGIN),
      ).toBeUndefined();
    });

    it("rejects vbscript: scheme", () => {
      expect(safeReturnTo("vbscript:msgbox(1)", ORIGIN)).toBeUndefined();
    });

    it("rejects file: scheme", () => {
      expect(safeReturnTo("file:///etc/passwd", ORIGIN)).toBeUndefined();
    });

    it("rejects uppercase HTTP:// scheme (not a canonical accepted prefix)", () => {
      // The implementation uses startsWith("http://") / startsWith("https://")
      // (lowercase), so uppercase schemes fall through to undefined.
      expect(
        safeReturnTo(`HTTP://${ORIGIN.replace("https://", "")}/device`, ORIGIN),
      ).toBeUndefined();
    });
  });

  // -------------------------------------------------------------------------
  // Reject cases — relative paths without leading slash
  // -------------------------------------------------------------------------

  describe("reject — relative paths without leading slash", () => {
    it("rejects bare relative path without leading slash", () => {
      expect(safeReturnTo("dashboard", ORIGIN)).toBeUndefined();
    });

    it("rejects relative traversal path without leading slash", () => {
      expect(safeReturnTo("../foo", ORIGIN)).toBeUndefined();
    });
  });

  // -------------------------------------------------------------------------
  // SSR mode — currentOrigin = ""
  // -------------------------------------------------------------------------

  describe("SSR mode (currentOrigin = '')", () => {
    it("accepts relative paths when currentOrigin is empty (SSR)", () => {
      expect(safeReturnTo("/foo", "")).toBe("/foo");
    });

    it("rejects absolute URLs when currentOrigin is empty (origin mismatch)", () => {
      expect(safeReturnTo(`${ORIGIN}/foo`, "")).toBeUndefined();
    });

    it("rejects empty string even in SSR mode", () => {
      expect(safeReturnTo("", "")).toBeUndefined();
    });
  });
});
