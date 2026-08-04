/**
 * The account switcher's decisions (#1488 Phase 3).
 *
 * Pinned here rather than through the sidebar's Radix dropdown: this repo has
 * no working pattern for opening one under a DOM emulator, and the dropdown is
 * not where the risk is. What can be wrong is *when* the list is re-read, what
 * happens when a switch is rejected, and whether the page is genuinely left.
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const mockListAccounts = vi.hoisted(() => vi.fn());
const mockSwitchAccount = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/accounts", () => ({
  listAccounts: (...a: unknown[]) => mockListAccounts(...a),
  switchAccount: (...a: unknown[]) => mockSwitchAccount(...a),
}));

import { useAccountSwitcher } from "./useAccountSwitcher";

const ALICE = {
  user_id: "alice",
  email: "alice@example.com",
  name: "Alice",
  picture: null,
  is_active: true,
};
const BOB = {
  user_id: "bob",
  email: "bob@example.com",
  name: "Bob",
  picture: null,
  is_active: false,
};

let assignSpy: ReturnType<typeof vi.fn>;
// Stubbing window.location without restoring it leaks into every test file
// that runs afterwards in the same worker — capture the descriptor and put it
// back.
const realLocation = Object.getOwnPropertyDescriptor(window, "location");

beforeEach(() => {
  vi.clearAllMocks();
  mockListAccounts.mockResolvedValue([ALICE, BOB]);
  mockSwitchAccount.mockResolvedValue(undefined);
  assignSpy = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      assign: assignSpy,
      origin: "https://app.test",
      href: "https://app.test/",
    },
  });
});

afterEach(() => {
  if (realLocation) {
    Object.defineProperty(window, "location", realLocation);
  }
  vi.restoreAllMocks();
});

describe("useAccountSwitcher", () => {
  it("starts empty and only reads when asked", () => {
    const { result } = renderHook(() => useAccountSwitcher());
    expect(result.current.accounts).toEqual([]);
    expect(mockListAccounts).not.toHaveBeenCalled();
  });

  it("refresh loads the session's accounts", async () => {
    const { result } = renderHook(() => useAccountSwitcher());
    await act(() => result.current.refresh());
    expect(result.current.accounts).toEqual([ALICE, BOB]);
  });

  it("refresh can be called repeatedly — the menu re-reads on every open", async () => {
    // A cached list would offer a switch the server rejects after another tab
    // signed an account out.
    const { result } = renderHook(() => useAccountSwitcher());
    await act(() => result.current.refresh());
    await act(() => result.current.refresh());
    expect(mockListAccounts).toHaveBeenCalledTimes(2);
  });

  it("degrades to an empty list when the endpoint is missing", async () => {
    // An older backend must leave the menu exactly as it was, never broken.
    mockListAccounts.mockRejectedValue(new Error("404"));
    const { result } = renderHook(() => useAccountSwitcher());
    await act(() => result.current.refresh());
    expect(result.current.accounts).toEqual([]);
  });

  it("switching leaves the page instead of soft-refreshing", async () => {
    // The active workspace is a per-user column and providers cache per-user
    // data; a client-side refresh would render the old workspace under the new
    // identity.
    const { result } = renderHook(() => useAccountSwitcher());
    await act(() => result.current.switchTo("bob"));
    expect(mockSwitchAccount).toHaveBeenCalledWith("bob");
    expect(assignSpy).toHaveBeenCalledWith("/");
  });

  it("a rejected switch re-reads the list and does NOT navigate", async () => {
    // 404 = the account is no longer on this session. Navigating would land on
    // a page rendered for an identity we no longer hold.
    mockSwitchAccount.mockRejectedValueOnce(new Error("404"));
    const { result } = renderHook(() => useAccountSwitcher());
    await act(() => result.current.switchTo("bob"));
    expect(assignSpy).not.toHaveBeenCalled();
    expect(mockListAccounts).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(result.current.switchingTo).toBeNull());
  });

  it("marks the in-flight account so the rows can disable", async () => {
    let resolveSwitch: () => void = () => {};
    mockSwitchAccount.mockReturnValue(
      new Promise<void>((r) => {
        resolveSwitch = r;
      }),
    );
    const { result } = renderHook(() => useAccountSwitcher());
    let pending: Promise<void>;
    act(() => {
      pending = result.current.switchTo("bob");
    });
    await waitFor(() => expect(result.current.switchingTo).toBe("bob"));
    await act(async () => {
      resolveSwitch();
      await pending;
    });
  });

  it("does not duplicate /api/v1 when the env var already carries it", async () => {
    // Some deployments set NEXT_PUBLIC_API_URL=https://api.example.com/api/v1.
    // Concatenating would yield /api/v1/api/v1/auth/... and break the flow;
    // buildOAuthRedirect strips the suffix, which is why it is used here
    // instead of string building.
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com/api/v1");
    const { result } = renderHook(() => useAccountSwitcher());
    act(() => result.current.addAccount());
    const url = assignSpy.mock.calls[0][0] as string;
    expect(url).not.toContain("/api/v1/api/v1");
    expect(url).toContain("https://api.example.com/api/v1/auth/google/login");
    vi.unstubAllEnvs();
  });

  it("the add flow carries the flag that makes the callback append", async () => {
    // Without add_account=1 the callback REPLACES the session and there is
    // never a second account to switch to.
    const { result } = renderHook(() => useAccountSwitcher());
    act(() => result.current.addAccount());
    const url = assignSpy.mock.calls[0][0] as string;
    expect(url).toContain("add_account=1");
    expect(url).toContain("/api/v1/auth/google/login");
    expect(url).toContain(encodeURIComponent("https://app.test/"));
  });
});
