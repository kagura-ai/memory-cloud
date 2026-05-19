import { describe, it, expect } from "vitest";
import { APP_VERSION } from "./version";
import packageJson from "../../package.json";

describe("APP_VERSION", () => {
  it("matches the version field in package.json", () => {
    expect(APP_VERSION).toBe(packageJson.version);
  });

  it("is a non-empty semver-like string", () => {
    expect(APP_VERSION).toMatch(/^\d+\.\d+\.\d+/);
  });
});
