import { describe, it, expect } from "vitest";

import { formatUserPartitionLabel } from "./sleep-report";

describe("formatUserPartitionLabel (#1201)", () => {
  it("returns the email when present", () => {
    expect(formatUserPartitionLabel("owner@test.com", "local:owner")).toBe(
      "owner@test.com",
    );
  });

  it("falls back to uid:<first8> when email is null (connector/non-human)", () => {
    expect(formatUserPartitionLabel(null, "connector-1234567890")).toBe(
      "uid:connecto",
    );
  });

  it("falls back when email is undefined", () => {
    expect(formatUserPartitionLabel(undefined, "abcdefghxyz")).toBe(
      "uid:abcdefgh",
    );
  });

  it("falls back when email is an empty string", () => {
    expect(formatUserPartitionLabel("", "shortid")).toBe("uid:shortid");
  });
});
