/**
 * Tests for WorkspaceProvider's reload trigger (#1532).
 *
 * `AuthContext.refetchUser()` hands back a fresh `user` object on every call,
 * even when nothing changed. The provider must reload workspaces only when the
 * values it actually reads (`user.id`, `user.current_workspace_id`) change —
 * re-running on object identity flips `loading`, which makes WorkspaceGuard
 * unmount the whole authenticated subtree and re-fire one-shot URL consumers.
 */

import { render, waitFor } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import type { User } from "@/lib/auth/auth";
import { WorkspaceProvider, useWorkspace } from "./WorkspaceContext";

const { authHolder, mockListWorkspaces } = vi.hoisted(() => ({
  authHolder: {
    current: {
      user: null as User | null,
      isLoading: false,
      refetchUser: vi.fn(),
    },
  },
  mockListWorkspaces: vi.fn(),
}));

vi.mock("./AuthContext", () => ({
  useAuth: () => authHolder.current,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@/lib/api/workspaces", () => ({
  listWorkspaces: () => mockListWorkspaces(),
  switchWorkspace: vi.fn(),
}));

const baseUser: User = {
  id: "u1",
  email: "u1@example.com",
  name: "U1",
  current_workspace_id: "w1",
};

const workspaces = [
  { id: "w1", name: "W1" },
  { id: "w2", name: "W2" },
];

function Probe() {
  const { currentWorkspaceId, loading } = useWorkspace();
  return (
    <div>
      <span data-testid="ws">{currentWorkspaceId ?? "none"}</span>
      <span data-testid="loading">{String(loading)}</span>
    </div>
  );
}

function renderProvider() {
  return render(
    <WorkspaceProvider>
      <Probe />
    </WorkspaceProvider>,
  );
}

beforeEach(() => {
  mockListWorkspaces.mockReset();
  mockListWorkspaces.mockResolvedValue(workspaces);
  authHolder.current = { user: baseUser, isLoading: false, refetchUser: vi.fn() };
});

describe("WorkspaceProvider reload trigger", () => {
  it("loads once on mount and selects the user's current workspace", async () => {
    const { getByTestId } = renderProvider();

    await waitFor(() => expect(getByTestId("ws").textContent).toBe("w1"));
    expect(mockListWorkspaces).toHaveBeenCalledTimes(1);
    expect(getByTestId("loading").textContent).toBe("false");
  });

  it("does NOT reload when refetchUser yields a new object with the same ids", async () => {
    const { getByTestId, rerender } = renderProvider();
    await waitFor(() => expect(getByTestId("ws").textContent).toBe("w1"));

    // Same id + same current_workspace_id, fresh object identity.
    authHolder.current = {
      ...authHolder.current,
      user: { ...baseUser, name: "U1 (re-synced)" },
    };
    rerender(
      <WorkspaceProvider>
        <Probe />
      </WorkspaceProvider>,
    );

    // Give any stray effect a tick to fire, then assert it did not.
    await new Promise((r) => setTimeout(r, 0));
    expect(mockListWorkspaces).toHaveBeenCalledTimes(1);
    expect(getByTestId("loading").textContent).toBe("false");
  });

  it("DOES reload when current_workspace_id changes", async () => {
    const { getByTestId, rerender } = renderProvider();
    await waitFor(() => expect(getByTestId("ws").textContent).toBe("w1"));

    authHolder.current = {
      ...authHolder.current,
      user: { ...baseUser, current_workspace_id: "w2" },
    };
    rerender(
      <WorkspaceProvider>
        <Probe />
      </WorkspaceProvider>,
    );

    await waitFor(() => expect(getByTestId("ws").textContent).toBe("w2"));
    expect(mockListWorkspaces).toHaveBeenCalledTimes(2);
  });

  it("DOES reload when the signed-in user changes", async () => {
    const { getByTestId, rerender } = renderProvider();
    await waitFor(() => expect(getByTestId("ws").textContent).toBe("w1"));

    authHolder.current = {
      ...authHolder.current,
      user: { ...baseUser, id: "u2" },
    };
    rerender(
      <WorkspaceProvider>
        <Probe />
      </WorkspaceProvider>,
    );

    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalledTimes(2));
  });
});
