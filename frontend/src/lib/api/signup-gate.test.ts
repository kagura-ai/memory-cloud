/**
 * Signup gate API client (#2831): the allowlist list reads one page,
 * `{items, total, limit, offset}`, and sends only the parameters given.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockGet = vi.fn();

vi.mock("./base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
  },
}));

import { listSignupAllowlist } from "./signup-gate";

const BASE = "/api/v1/admin/signup-gate/allowlist";
const PAGE = { items: [], total: 0, limit: 50, offset: 0 };

beforeEach(() => {
  mockGet.mockReset();
  mockGet.mockResolvedValue(PAGE);
});

describe("listSignupAllowlist", () => {
  it("sends no query string without parameters and returns the page", async () => {
    await expect(listSignupAllowlist()).resolves.toEqual(PAGE);
    expect(mockGet).toHaveBeenCalledWith(BASE);
  });

  it("sends the paging parameters", async () => {
    await listSignupAllowlist({ limit: 50, offset: 100 });
    expect(mockGet).toHaveBeenCalledWith(`${BASE}?limit=50&offset=100`);
  });

  it("encodes the search text and sends the exact filters", async () => {
    await listSignupAllowlist({
      search: "50% _a&b",
      provider: "email",
      source: "beta_invite",
      state: "revoked",
    });
    expect(mockGet).toHaveBeenCalledWith(
      `${BASE}?search=50%25+_a%26b&provider=email&source=beta_invite&state=revoked`,
    );
  });

  it("leaves out an empty search", async () => {
    await listSignupAllowlist({ offset: 0, search: "" });
    expect(mockGet).toHaveBeenCalledWith(`${BASE}?offset=0`);
  });
});
