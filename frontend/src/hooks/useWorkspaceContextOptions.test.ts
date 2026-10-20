/**
 * Tests for useWorkspaceContextOptions (#2129).
 */

import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useWorkspaceContextOptions } from "./useWorkspaceContextOptions";

let workspaceId: string | null = "ws-1";
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspaceId: workspaceId }),
}));

const mockGetContexts = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...a: unknown[]) => mockGetContexts(...a),
}));

beforeEach(() => {
  vi.clearAllMocks();
  workspaceId = "ws-1";
});

describe("useWorkspaceContextOptions", () => {
  it("maps contexts to options labelled by display name, else name", async () => {
    mockGetContexts.mockResolvedValue({
      contexts: [
        { id: "a", name: "alpha", display_name: "Alpha" },
        { id: "b", name: "beta", display_name: null },
      ],
      total: 2,
    });
    const { result } = renderHook(() => useWorkspaceContextOptions());
    await waitFor(() =>
      expect(result.current).toEqual([
        { id: "a", label: "Alpha" },
        { id: "b", label: "beta" },
      ]),
    );
  });

  it("empties the options when there is no current workspace", async () => {
    mockGetContexts.mockResolvedValue({
      contexts: [{ id: "a", name: "alpha", display_name: null }],
      total: 1,
    });
    const { result, rerender } = renderHook(() =>
      useWorkspaceContextOptions(),
    );
    await waitFor(() => expect(result.current).toHaveLength(1));

    workspaceId = null;
    rerender();
    await waitFor(() => expect(result.current).toEqual([]));
  });

  it("leaves the options empty when the fetch fails", async () => {
    mockGetContexts.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useWorkspaceContextOptions());
    await waitFor(() => expect(mockGetContexts).toHaveBeenCalled());
    expect(result.current).toEqual([]);
  });
});
