/**
 * Beta invite API client (#1582, #1595): the wrappers hit the right routes,
 * a create without a label sends NO body (the pre-#1595 request, byte for
 * byte), and reissue is a body-less POST on the invite's own path.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();

vi.mock("./base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

import {
  createBetaInvite,
  getMyBetaInvites,
  previewBetaInvite,
  reissueBetaInvite,
  revokeBetaInvite,
} from "./beta-invites";

const BASE = "/api/v1/beta-invites";
const CREATED = {
  id: "inv-2",
  url: "https://app.example.com/join/tok_secret",
  expires_at: "2030-01-08T00:00:00Z",
  label: null,
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockDelete.mockReset();
  mockPost.mockResolvedValue(CREATED);
});

describe("createBetaInvite", () => {
  it.each([
    ["no argument", undefined],
    ["null", null],
    ["an empty string", ""],
    ["whitespace only", "   "],
  ])("sends no body at all for %s", async (_name, label) => {
    await createBetaInvite(label);
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost.mock.calls[0]).toEqual([BASE]);
  });

  it("sends the trimmed label", async () => {
    mockPost.mockResolvedValue({ ...CREATED, label: "Alice" });
    const created = await createBetaInvite("  Alice ");
    expect(mockPost).toHaveBeenCalledWith(BASE, { label: "Alice" });
    expect(created.label).toBe("Alice");
  });
});

describe("reissueBetaInvite", () => {
  it("POSTs the invite's reissue path with no body and returns the new URL", async () => {
    const created = await reissueBetaInvite("inv/1");
    expect(mockPost.mock.calls[0]).toEqual([`${BASE}/inv%2F1/reissue`]);
    expect(created).toEqual(CREATED);
  });
});

describe("the other wrappers", () => {
  it("reads the summary", async () => {
    const summary = {
      quota: 4,
      used: 1,
      active: 1,
      redeemed: 0,
      remaining: 3,
      invites: [],
    };
    mockGet.mockResolvedValue(summary);
    expect(await getMyBetaInvites()).toEqual(summary);
    expect(mockGet).toHaveBeenCalledWith(`${BASE}/me`);
  });

  it("revokes by id", async () => {
    mockDelete.mockResolvedValue(undefined);
    await revokeBetaInvite("inv-1");
    expect(mockDelete).toHaveBeenCalledWith(`${BASE}/inv-1`);
  });

  it("previews by token", async () => {
    mockGet.mockResolvedValue({ valid: true, expires_at: "x" });
    await previewBetaInvite("tok");
    expect(mockGet).toHaveBeenCalledWith(`${BASE}/tok/preview`);
  });
});
