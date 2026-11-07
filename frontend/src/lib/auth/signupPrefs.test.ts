import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { signupPrefs } from "./signupPrefs";

/** Make `Intl.DateTimeFormat()` report `timeZone` (or throw). */
function stubTimeZone(timeZone: unknown | Error) {
  vi.spyOn(Intl, "DateTimeFormat").mockImplementation(function () {
    if (timeZone instanceof Error) throw timeZone;
    return { resolvedOptions: () => ({ timeZone }) };
  } as unknown as typeof Intl.DateTimeFormat);
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("signupPrefs — the sign-in page's language (#2700)", () => {
  it("is the language chosen on the sign-in page", () => {
    localStorage.setItem("kagura_locale", "ja");
    stubTimeZone("Asia/Tokyo");
    expect(signupPrefs().locale).toBe("ja");
  });

  it("falls back to en when nothing usable is stored", () => {
    localStorage.setItem("kagura_locale", "fr");
    vi.spyOn(navigator, "language", "get").mockReturnValue("de-DE");
    stubTimeZone("UTC");
    expect(signupPrefs().locale).toBe("en");
  });

  it("falls back to en when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    stubTimeZone("UTC");
    expect(signupPrefs().locale).toBe("en");
  });
});

describe("signupPrefs — the browser's time zone (#2700)", () => {
  it("is the resolved IANA time zone", () => {
    stubTimeZone("America/New_York");
    expect(signupPrefs()).toEqual({
      locale: "en",
      timezone: "America/New_York",
    });
  });

  it("is omitted when Intl throws", () => {
    stubTimeZone(new RangeError("no tz support"));
    expect(signupPrefs()).toEqual({ locale: "en" });
  });

  it.each([
    ["undefined", undefined],
    ["empty", ""],
    ["not a string", 540],
    ["longer than 50 characters", `America/${"A".repeat(43)}`],
  ])("is omitted when it is %s", (_label, value) => {
    stubTimeZone(value);
    expect(signupPrefs()).toEqual({ locale: "en" });
  });

  it("keeps a 50-character name", () => {
    const fifty = `America/${"A".repeat(42)}`;
    stubTimeZone(fifty);
    expect(signupPrefs().timezone).toBe(fifty);
  });
});
