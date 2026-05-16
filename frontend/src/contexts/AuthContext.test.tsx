/**
 * Tests for AuthContext silent-refresh discipline (issue #678).
 *
 * Regression guard for the "Context tree-killer" pattern: `refetchUser` MUST
 * NOT flip `isLoading`, otherwise the authenticated subtree under
 * DashboardContent's spinner gate (layout.tsx) unmounts and destroys
 * WorkspaceProvider state mid-flow.
 */

import { render, screen, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { AuthProvider, useAuth } from "./AuthContext";
import type { User } from "@/lib/auth/auth";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const mockGetCurrentUser = vi.fn();
const mockLogout = vi.fn();
vi.mock("@/lib/auth/auth", async () => ({
  ...(await vi.importActual<typeof import("@/lib/auth/auth")>(
    "@/lib/auth/auth",
  )),
  getCurrentUser: () => mockGetCurrentUser(),
  logout: () => mockLogout(),
}));

const USER_A: User = {
  id: "user-a",
  email: "a@example.com",
  name: "User A",
  picture: "",
  role: "user",
};

const USER_B: User = {
  id: "user-b",
  email: "b@example.com",
  name: "User B",
  picture: "",
  role: "admin",
};

/** Probe component: surfaces auth state into the DOM and exposes refetchUser. */
function Probe({ loadingLog }: { loadingLog: Array<boolean> }) {
  const { user, isLoading, refetchUser } = useAuth();
  loadingLog.push(isLoading);
  return (
    <>
      <div data-testid="user">{user ? user.email : "null"}</div>
      <div data-testid="loading">{isLoading ? "true" : "false"}</div>
      <button
        data-testid="refetch"
        onClick={() => {
          void refetchUser();
        }}
      >
        refetch
      </button>
    </>
  );
}

describe("AuthContext silent refresh (issue #678)", () => {
  beforeEach(() => {
    mockGetCurrentUser.mockReset();
    mockLogout.mockReset();
  });

  it("initial mount flips isLoading true -> false (mount-time gate preserved)", async () => {
    mockGetCurrentUser.mockResolvedValueOnce(USER_A);
    const log: boolean[] = [];

    render(
      <AuthProvider>
        <Probe loadingLog={log} />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });

    // The very first render saw isLoading=true (mount-time gate).
    expect(log[0]).toBe(true);
    expect(log[log.length - 1]).toBe(false);
    expect(screen.getByTestId("user").textContent).toBe(USER_A.email);
  });

  it("refetchUser does NOT flip isLoading (silent refresh — regression guard)", async () => {
    mockGetCurrentUser.mockResolvedValueOnce(USER_A);
    const log: boolean[] = [];

    render(
      <AuthProvider>
        <Probe loadingLog={log} />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });

    // Reset the log AFTER mount settles. We only care about post-mount behavior.
    log.length = 0;

    // Second call returns a different user — refetchUser should swap the user
    // without ever flipping isLoading to true.
    mockGetCurrentUser.mockResolvedValueOnce(USER_B);

    await act(async () => {
      screen.getByTestId("refetch").click();
    });

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe(USER_B.email);
    });

    // CORE ASSERTION: isLoading was never true during refetch.
    expect(log.every((v) => v === false)).toBe(true);
  });

  it("refetchUser transient error preserves user (does NOT setUser(null))", async () => {
    mockGetCurrentUser.mockResolvedValueOnce(USER_A);

    render(
      <AuthProvider>
        <Probe loadingLog={[]} />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe(USER_A.email);
    });

    // Simulate a transient network / 5xx error.
    mockGetCurrentUser.mockRejectedValueOnce(new Error("network blip"));

    await act(async () => {
      screen.getByTestId("refetch").click();
    });

    // Give the rejected promise a tick to settle.
    await waitFor(() => {
      expect(mockGetCurrentUser).toHaveBeenCalledTimes(2);
    });

    // User remains USER_A — transient errors must not log the user out.
    expect(screen.getByTestId("user").textContent).toBe(USER_A.email);
  });

  it("refetchUser observing 401 (getCurrentUser returns null) clears user", async () => {
    mockGetCurrentUser.mockResolvedValueOnce(USER_A);

    render(
      <AuthProvider>
        <Probe loadingLog={[]} />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe(USER_A.email);
    });

    // getCurrentUser swallows 401 and returns null — see its public-contract
    // docblock in lib/auth/auth.ts.
    mockGetCurrentUser.mockResolvedValueOnce(null);

    await act(async () => {
      screen.getByTestId("refetch").click();
    });

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("null");
    });
  });
});
