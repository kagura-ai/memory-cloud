/**
 * #1227: URL-scope hook for the admin memory-health drill-down.
 *
 * The same-value guard matters for history integrity: without it a fast
 * double-click on a context row (router.push commits in a transition, so
 * the list can still be interactive when the second click lands) pushes
 * duplicate identical entries and the first browser Back press appears
 * dead. Mirrors the useTabParam.test.ts mock convention.
 */

import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { useContextScopeParam } from "./useContextScopeParam";

const { mockGet, mockPush, mockToString } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPush: vi.fn(),
  mockToString: vi.fn(() => ""),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => ({ get: mockGet, toString: mockToString }),
  usePathname: () => "/admin/memory-health",
  useRouter: () => ({ push: mockPush }),
}));

beforeEach(() => {
  mockGet.mockReset();
  mockPush.mockReset();
  mockToString.mockReset().mockReturnValue("");
});

describe("useContextScopeParam", () => {
  it("reads the scope from the URL", () => {
    mockGet.mockReturnValue("ctx-1");
    const { result } = renderHook(() => useContextScopeParam());
    expect(result.current[0]).toBe("ctx-1");
  });

  it("pushes the scope into the URL (navigation, not replace)", () => {
    mockGet.mockReturnValue(null);
    const { result } = renderHook(() => useContextScopeParam());

    act(() => {
      result.current[1]("ctx-1");
    });

    expect(mockPush).toHaveBeenCalledWith("/admin/memory-health?context_id=ctx-1");
  });

  it("clearing the scope pushes the bare pathname", () => {
    mockGet.mockReturnValue("ctx-1");
    mockToString.mockReturnValue("context_id=ctx-1");
    const { result } = renderHook(() => useContextScopeParam());

    act(() => {
      result.current[1](null);
    });

    expect(mockPush).toHaveBeenCalledWith("/admin/memory-health");
  });

  it("setting the CURRENT scope again is a no-op (no duplicate history entry)", () => {
    mockGet.mockReturnValue("ctx-1");
    const { result } = renderHook(() => useContextScopeParam());

    act(() => {
      result.current[1]("ctx-1");
    });

    expect(mockPush).not.toHaveBeenCalled();
  });

  it("clearing an already-absent scope is a no-op", () => {
    mockGet.mockReturnValue(null);
    const { result } = renderHook(() => useContextScopeParam());

    act(() => {
      result.current[1](null);
    });

    expect(mockPush).not.toHaveBeenCalled();
  });

  it("preserves unrelated params when setting the scope", () => {
    mockGet.mockReturnValue(null);
    mockToString.mockReturnValue("tab=overview");
    const { result } = renderHook(() => useContextScopeParam());

    act(() => {
      result.current[1]("ctx-1");
    });

    expect(mockPush).toHaveBeenCalledWith("/admin/memory-health?tab=overview&context_id=ctx-1");
  });
});
