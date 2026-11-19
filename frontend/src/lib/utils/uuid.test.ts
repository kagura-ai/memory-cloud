import { describe, expect, it } from "vitest";
import { isUuid } from "./uuid";

describe("isUuid", () => {
  it("accepts a canonical UUID in either case", () => {
    expect(isUuid("abfd654d-c489-47fe-a1d3-e6471041259b")).toBe(true);
    expect(isUuid("ABFD654D-C489-47FE-A1D3-E6471041259B")).toBe(true);
  });

  it("rejects anything else", () => {
    expect(isUuid(null)).toBe(false);
    expect(isUuid(undefined)).toBe(false);
    expect(isUuid("")).toBe(false);
    expect(isUuid("ctx-1")).toBe(false);
    expect(isUuid("abfd654d-c489-47fe-a1d3-e6471041259b/..")).toBe(false);
    expect(isUuid(" abfd654d-c489-47fe-a1d3-e6471041259b")).toBe(false);
    expect(isUuid("abfd654dc48947fea1d3e6471041259b")).toBe(false);
  });
});
