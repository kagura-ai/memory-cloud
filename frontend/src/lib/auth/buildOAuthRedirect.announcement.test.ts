import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildOAuthRedirect } from "./buildOAuthRedirect";

// #2761: the announcement-email opt-in rides the OAuth login URL after the
// sign-in page's prefs, and only when the page showed the checkbox.

const FRONTEND_ORIGIN = "http://localhost:3000";
const PREFS = "&locale=en&timezone=Asia%2FTokyo";

beforeEach(() => {
  localStorage.setItem("kagura_locale", "en");
  vi.spyOn(Intl, "DateTimeFormat").mockImplementation(function () {
    return { resolvedOptions: () => ({ timeZone: "Asia/Tokyo" }) };
  } as unknown as typeof Intl.DateTimeFormat);
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("buildOAuthRedirect — announcement-email opt-in (#2761)", () => {
  it.each([
    [true, "true"],
    [false, "false"],
  ])("appends the answer %s and the version after the prefs", (value, raw) => {
    const url = buildOAuthRedirect("google", "/", {
      acceptedTerms: "2026-09",
      announcementEmails: { value, version: "2026-11" },
    });
    expect(url).toBe(
      `https://api.example.com/api/v1/auth/google/login?return_to=${encodeURIComponent(`${FRONTEND_ORIGIN}/`)}&accepted_terms=2026-09${PREFS}&announcement_emails=${raw}&announcement_consent_version=2026-11`,
    );
  });

  it("URL-encodes the version", () => {
    const url = buildOAuthRedirect("github", "/", {
      announcementEmails: { value: true, version: "a&b" },
    });
    expect(new URL(url).searchParams.get("announcement_consent_version")).toBe(
      "a&b",
    );
  });

  it.each([
    ["absent", {}],
    ["undefined", { announcementEmails: undefined }],
    ["an empty version", { announcementEmails: { value: true, version: "" } }],
  ])("is byte-identical to the form without it when %s", (_l, options) => {
    expect(buildOAuthRedirect("google", "/x", options)).toBe(
      buildOAuthRedirect("google", "/x"),
    );
  });
});
