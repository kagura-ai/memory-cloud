/**
 * Tests for WorkspaceProvider's reload trigger (#1532).
 *
 * `AuthContext.refetchUser()` hands back a fresh `user` object on every call,
 * even when nothing changed. The provider must reload workspaces only when the
 * values it actually reads (`user.id`, `user.current_workspace_id`) change —
 * re-running on object identity flips `loading`, which makes WorkspaceGuard
 * unmount the whole authenticated subtree and re-fire one-shot URL consumers.
 */

import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { ApiError } from "@/lib/api/base";
import type { User } from "@/lib/auth/auth";
import { WorkspaceProvider, useWorkspace } from "./WorkspaceContext";

const { authHolder, mockListWorkspaces, mockSwitch } = vi.hoisted(() => ({
  authHolder: {
    current: {
      user: null as User | null,
      isLoading: false,
      refetchUser: vi.fn(),
    },
  },
  mockListWorkspaces: vi.fn(),
  mockSwitch: vi.fn(),
}));

vi.mock("./AuthContext", () => ({
  useAuth: () => authHolder.current,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@/lib/api/workspaces", () => ({
  listWorkspaces: () => mockListWorkspaces(),
  switchWorkspace: (id: string) => mockSwitch(id),
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

let savedRefresh: (() => Promise<void>) | undefined;
function Probe() {
  const { currentWorkspaceId, loading, loadFailed, switchWorkspace, refreshWorkspaces } = useWorkspace();
  return (
    <div>
      <span data-testid="ws">{currentWorkspaceId ?? "none"}</span>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="loadFailed">{String(loadFailed)}</span>
      <button onClick={() => { savedRefresh = refreshWorkspaces; }}>capture</button>
      <button onClick={() => void switchWorkspace("w2").catch(() => {})}>switch</button>
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
  mockSwitch.mockReset();
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

  it("withholds protected children while switching, then selects the explicit target", async () => {
    let switched!: () => void;
    mockSwitch.mockImplementationOnce(() => new Promise<void>(resolve => { switched = resolve; }));
    authHolder.current.refetchUser.mockResolvedValue({ ...baseUser, current_workspace_id: "w2" });
    const page = renderProvider();
    await waitFor(() => expect(page.getByTestId("ws").textContent).toBe("w1"));
    fireEvent.click(page.getByRole("button", { name: "capture" }));
    fireEvent.click(page.getByRole("button", { name: "switch" }));
    expect(page.getByTestId("loading").textContent).toBe("true");
    await act(async () => { switched(); });
    expect(page.getByTestId("ws").textContent).toBe("w2");
    // The callback captured by a save on A cannot select A after B is confirmed.
    await act(async () => { await savedRefresh!(); });
    expect(page.getByTestId("ws").textContent).toBe("w2");
    expect(page.getByTestId("loading").textContent).toBe("false");
  });

  it("accepts the confirmed B list when /auth/me publishes B before the list completes", async () => {
    const page = renderProvider();
    await waitFor(() => expect(page.getByTestId("ws")).toHaveTextContent("w1"));
    const confirmed = { ...baseUser, current_workspace_id: "w2" };
    authHolder.current.refetchUser.mockResolvedValue(confirmed);
    let finish!: (result: typeof workspaces) => void;
    mockListWorkspaces.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    fireEvent.click(page.getByRole("button", { name: "switch" }));
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalledTimes(2));
    authHolder.current.user = confirmed;
    page.rerender(<WorkspaceProvider><Probe /></WorkspaceProvider>);
    await act(async () => { finish(workspaces); });
    expect(page.getByTestId("ws")).toHaveTextContent("w2");
    expect(page.getByTestId("loading")).toHaveTextContent("false");
  });

  it("does not interpret a failed initial list as no workspaces", async () => {
    mockListWorkspaces.mockRejectedValueOnce(new Error("offline"));
    const page = renderProvider();
    await waitFor(() => expect(page.getByTestId("loadFailed").textContent).toBe("true"));
    expect(page.getByTestId("ws").textContent).toBe("none");
  });

  it.each([403, 404, 410])("retains A and refreshes memberships when B refuses the switch (%i)", async status => {
    authHolder.current.refetchUser.mockResolvedValueOnce(baseUser);
    mockSwitch.mockRejectedValueOnce(new ApiError({ status, message: "Membership removed" }));
    mockListWorkspaces.mockResolvedValueOnce(workspaces).mockResolvedValueOnce([workspaces[0]]);
    const page = renderProvider();
    await waitFor(() => expect(page.getByTestId("ws")).toHaveTextContent("w1"));
    fireEvent.click(page.getByRole("button", { name: "switch" }));
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalledTimes(2));
    expect(page.getByTestId("loadFailed")).toHaveTextContent("false");
    expect(page.getByTestId("ws")).toHaveTextContent("w1");
    expect(authHolder.current.refetchUser).toHaveBeenCalledTimes(1);
  });

  it("fails closed if another tab selected C before B refused the switch", async () => {
    mockSwitch.mockRejectedValueOnce(new ApiError({ status: 403, message: "Membership removed" }));
    authHolder.current.refetchUser.mockResolvedValueOnce({ ...baseUser, current_workspace_id: "w3" });
    const page = renderProvider();
    await waitFor(() => expect(page.getByTestId("ws")).toHaveTextContent("w1"));
    fireEvent.click(page.getByRole("button", { name: "switch" }));
    await waitFor(() => expect(page.getByTestId("loadFailed")).toHaveTextContent("true"));
    expect(page.getByTestId("ws")).toHaveTextContent("none");
  });

  it.each(["switch response lost", "identity unavailable"])("fails closed: %s", async (failure) => {
    if (failure === "switch response lost") mockSwitch.mockRejectedValueOnce(new Error("offline"));
    else authHolder.current.refetchUser.mockResolvedValueOnce(undefined);
    const page = renderProvider();
    await waitFor(() => expect(page.getByTestId("ws").textContent).toBe("w1"));
    fireEvent.click(page.getByRole("button", { name: "switch" }));
    await waitFor(() => expect(page.getByTestId("loadFailed").textContent).toBe("true"));
    expect(page.getByTestId("ws").textContent).toBe("none");
  });
  it("does not implicitly select the first membership when the current workspace is null", async () => {
    authHolder.current.user = { ...baseUser, current_workspace_id: null };
    const page = renderProvider();
    await waitFor(() => expect(page.getByTestId("loading").textContent).toBe("false"));
    expect(page.getByTestId("ws").textContent).toBe("none");
  });
  it("does not substitute an owned workspace when the selected id is inaccessible", async () => {
    authHolder.current.user = { ...baseUser, current_workspace_id: "removed" };
    const page = renderProvider();
    await waitFor(() => expect(page.getByTestId("loading").textContent).toBe("false"));
    expect(page.getByTestId("ws").textContent).toBe("none");
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
