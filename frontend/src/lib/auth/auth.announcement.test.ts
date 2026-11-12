import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockApiClientGet = vi.fn();

vi.mock("../api/base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockApiClientGet(...args),
    post: vi.fn(),
    delete: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import { getAuthUrl, getGitHubAuthUrl } from "./auth";

// #2761: the JSON-mode OAuth login carries the announcement-email opt-in after
// the prefs, and only when the page showed the checkbox.

beforeEach(() => {
  mockApiClientGet.mockReset();
  mockApiClientGet.mockResolvedValue({ authorization_url: "https://idp/" });
  localStorage.setItem("kagura_locale", "en");
  vi.spyOn(Intl, "DateTimeFormat").mockImplementation(function () {
    return { resolvedOptions: () => ({ timeZone: "Asia/Tokyo" }) };
  } as unknown as typeof Intl.DateTimeFormat);
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("getAuthUrl / getGitHubAuthUrl — announcement-email opt-in (#2761)", () => {
  it("Google: appends the answer after the prefs", async () => {
    await getAuthUrl("2026-09", { value: true, version: "2026-11" });
    expect(mockApiClientGet).toHaveBeenLastCalledWith(
      "/api/v1/auth/google/login?accepted_terms=2026-09&locale=en&timezone=Asia%2FTokyo&announcement_emails=true&announcement_consent_version=2026-11",
    );
  });

  it("GitHub: an unchecked box is sent as false", async () => {
    await getGitHubAuthUrl(undefined, { value: false, version: "2026-11" });
    expect(mockApiClientGet).toHaveBeenLastCalledWith(
      "/api/v1/auth/github/login?locale=en&timezone=Asia%2FTokyo&announcement_emails=false&announcement_consent_version=2026-11",
    );
  });

  it("without an answer the URL is unchanged", async () => {
    await getAuthUrl("2026-09");
    expect(mockApiClientGet).toHaveBeenLastCalledWith(
      "/api/v1/auth/google/login?accepted_terms=2026-09&locale=en&timezone=Asia%2FTokyo",
    );
  });
});
