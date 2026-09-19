/**
 * The shared beta-invite summary (#1582): when it may ask the server at all,
 * and that every mutation re-reads the numbers the sidebar card, the account
 * menu entry and the dialog all render from.
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockGetMine = vi.hoisted(() => vi.fn());
const mockCreate = vi.hoisted(() => vi.fn());
const mockRevoke = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/beta-invites", () => ({
  getMyBetaInvites: (...a: unknown[]) => mockGetMine(...a),
  createBetaInvite: (...a: unknown[]) => mockCreate(...a),
  revokeBetaInvite: (...a: unknown[]) => mockRevoke(...a),
}));

let mockFeatures: Record<string, boolean> | null = { beta_invites: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

import { ApiError } from "@/lib/api/base";
import { useBetaInvites } from "./useBetaInvites";

const SUMMARY = { quota: 4, used: 1, remaining: 3, invites: [] };
const CREATED = {
  id: "inv-2",
  url: "https://app.example.com/join/tok_secret",
  expires_at: "2030-01-08T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  mockFeatures = { beta_invites: true };
  mockGetMine.mockResolvedValue(SUMMARY);
  mockCreate.mockResolvedValue(CREATED);
  mockRevoke.mockResolvedValue(undefined);
});

describe("useBetaInvites — when it asks", () => {
  it.each([
    ["the flag is off", { beta_invites: false }],
    ["an older backend sends no flag", { plan_page: true }],
    ["the flags are still loading", null],
  ])("makes no request when %s", async (_label, features) => {
    mockFeatures = features;
    const { result } = renderHook(() => useBetaInvites());
    await act(async () => {});
    expect(mockGetMine).not.toHaveBeenCalled();
    expect(result.current.summary).toBeNull();
    expect(result.current.isLoading).toBe(false);
  });

  it("loads the summary once the flag is on", async () => {
    const { result } = renderHook(() => useBetaInvites());
    expect(result.current.summary).toBeNull();
    await waitFor(() => expect(result.current.summary).toEqual(SUMMARY));
    expect(result.current.isLoading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(mockGetMine).toHaveBeenCalledTimes(1);
  });

  it("reports a failed load and leaves the summary unknown", async () => {
    const failure = new ApiError({ message: "boom", status: 500 });
    mockGetMine.mockRejectedValueOnce(failure);
    const { result } = renderHook(() => useBetaInvites());
    await waitFor(() => expect(result.current.error).toBe(failure));
    expect(result.current.summary).toBeNull();
    expect(result.current.isLoading).toBe(false);
  });
});

describe("useBetaInvites — mutations refresh the shared summary", () => {
  it("create returns the one-time URL and re-reads the summary", async () => {
    const { result } = renderHook(() => useBetaInvites());
    await waitFor(() => expect(result.current.summary).toEqual(SUMMARY));

    const after = { ...SUMMARY, used: 2, remaining: 2 };
    mockGetMine.mockResolvedValueOnce(after);
    let created: typeof CREATED | undefined;
    await act(async () => {
      created = await result.current.create();
    });

    expect(created).toEqual(CREATED);
    expect(mockCreate).toHaveBeenCalledTimes(1);
    expect(result.current.summary).toEqual(after);
  });

  it("a 409 on create still re-reads the summary, then rethrows", async () => {
    const { result } = renderHook(() => useBetaInvites());
    await waitFor(() => expect(result.current.summary).toEqual(SUMMARY));

    const conflict = new ApiError({ message: "quota_exceeded", status: 409 });
    mockCreate.mockRejectedValueOnce(conflict);
    const atCap = { ...SUMMARY, used: 4, remaining: 0 };
    mockGetMine.mockResolvedValueOnce(atCap);

    let thrown: unknown;
    await act(async () => {
      try {
        await result.current.create();
      } catch (e) {
        thrown = e;
      }
    });

    expect(thrown).toBe(conflict);
    expect(result.current.summary).toEqual(atCap);
  });

  it("revoke re-reads the summary", async () => {
    const { result } = renderHook(() => useBetaInvites());
    await waitFor(() => expect(result.current.summary).toEqual(SUMMARY));

    const after = { ...SUMMARY, used: 0, remaining: 4 };
    mockGetMine.mockResolvedValueOnce(after);
    await act(async () => {
      await result.current.revoke("inv-1");
    });

    expect(mockRevoke).toHaveBeenCalledWith("inv-1");
    expect(result.current.summary).toEqual(after);
  });
});
