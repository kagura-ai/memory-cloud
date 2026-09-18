/**
 * Tests for useConsumeSearchParams (#1382).
 *
 * The hook centralizes the read-params → act → strip pattern shared by the
 * profile and connectors pages: a `consume` callback inspects the current
 * search params, acts (toast etc.) and returns true when it handled them;
 * the hook then marks the group handled (exactly once, safe under React
 * strict-mode double-invoked effects) and strips the params via
 * `router.replace(cleanUrl)`.
 */

import { renderHook } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  useConsumeSearchParams,
  resetConsumedSearchParams,
} from "./useConsumeSearchParams";

const { mockReplace, paramsHolder } = vi.hoisted(() => ({
  mockReplace: vi.fn(),
  paramsHolder: { current: new URLSearchParams() },
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => paramsHolder.current,
  useRouter: () => ({ replace: mockReplace }),
}));

beforeEach(() => {
  mockReplace.mockReset();
  paramsHolder.current = new URLSearchParams();
  resetConsumedSearchParams();
});

describe("useConsumeSearchParams", () => {
  it("calls consume with the params and strips via router.replace when consumed", () => {
    paramsHolder.current = new URLSearchParams("slack_error=failed");
    const consume = vi.fn().mockReturnValue(true);

    renderHook(() =>
      useConsumeSearchParams(consume, { cleanUrl: "/connectors" }),
    );

    expect(consume).toHaveBeenCalledTimes(1);
    expect(consume.mock.calls[0][0].get("slack_error")).toBe("failed");
    expect(mockReplace).toHaveBeenCalledWith("/connectors");
  });

  it("does not strip when consume returns false, and retries on params change", () => {
    const consume = vi.fn().mockReturnValue(false);

    const { rerender } = renderHook(() =>
      useConsumeSearchParams(consume, { cleanUrl: "/x" }),
    );
    expect(mockReplace).not.toHaveBeenCalled();

    // New params arrive (e.g. client-side navigation) → consume runs again.
    paramsHolder.current = new URLSearchParams("linked=1");
    consume.mockReturnValue(true);
    rerender();

    expect(consume).toHaveBeenCalledTimes(2);
    expect(mockReplace).toHaveBeenCalledWith("/x");
  });

  it("defers consumption until enabled becomes true", () => {
    paramsHolder.current = new URLSearchParams("slack_error=expired");
    const consume = vi.fn().mockReturnValue(true);

    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useConsumeSearchParams(consume, { enabled, cleanUrl: "/c" }),
      { initialProps: { enabled: false } },
    );
    expect(consume).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();

    rerender({ enabled: true });

    expect(consume).toHaveBeenCalledTimes(1);
    expect(mockReplace).toHaveBeenCalledWith("/c");
  });

  it("consumes exactly once — later rerenders and param changes are ignored", () => {
    paramsHolder.current = new URLSearchParams("refreshed=1");
    const consume = vi.fn().mockReturnValue(true);

    const { rerender } = renderHook(() =>
      useConsumeSearchParams(consume, { cleanUrl: "/profile" }),
    );
    expect(consume).toHaveBeenCalledTimes(1);

    rerender();
    paramsHolder.current = new URLSearchParams("refreshed=1&extra=x");
    rerender();

    expect(consume).toHaveBeenCalledTimes(1);
    expect(mockReplace).toHaveBeenCalledTimes(1);
  });

  it("uses the latest consume closure (no stale captures)", () => {
    const seen: string[] = [];
    const { rerender } = renderHook(
      ({ label }: { label: string }) =>
        useConsumeSearchParams(
          () => {
            seen.push(label);
            return label === "second";
          },
          { cleanUrl: "/y" },
        ),
      { initialProps: { label: "first" } },
    );

    paramsHolder.current = new URLSearchParams("a=1");
    rerender({ label: "second" });

    expect(seen).toEqual(["first", "second"]);
    expect(mockReplace).toHaveBeenCalledWith("/y");
  });

  // #1532: the consumer can be unmounted and remounted while the URL strip is
  // still in flight (a parent flipped a loading flag). The remount must not
  // re-consume the same params — but a *later* arrival of the same params,
  // after the strip landed, is a new event and must be consumed again.
  it("does not re-consume identical params on remount (strip still in flight)", () => {
    paramsHolder.current = new URLSearchParams("refreshed=1");
    const consume = vi.fn().mockReturnValue(true);

    const first = renderHook(() =>
      useConsumeSearchParams(consume, { cleanUrl: "/profile" }),
    );
    expect(consume).toHaveBeenCalledTimes(1);
    first.unmount();

    // Remount with ?refreshed=1 still in the URL.
    renderHook(() => useConsumeSearchParams(consume, { cleanUrl: "/profile" }));

    expect(consume).toHaveBeenCalledTimes(1);
    expect(mockReplace).toHaveBeenCalledTimes(1);
  });

  it("consumes the same params again once the strip has landed in between", () => {
    paramsHolder.current = new URLSearchParams("refreshed=1");
    const consume = vi.fn().mockReturnValue(true);

    const first = renderHook(() =>
      useConsumeSearchParams(consume, { cleanUrl: "/profile" }),
    );
    expect(consume).toHaveBeenCalledTimes(1);

    // The router.replace(cleanUrl) lands: params are now empty.
    paramsHolder.current = new URLSearchParams();
    first.rerender();
    first.unmount();

    // A second re-sync round trip arrives with the same params.
    paramsHolder.current = new URLSearchParams("refreshed=1");
    renderHook(() => useConsumeSearchParams(consume, { cleanUrl: "/profile" }));

    expect(consume).toHaveBeenCalledTimes(2);
    expect(mockReplace).toHaveBeenCalledTimes(2);
  });

  it("forgets a stale remount memory after the TTL, so a late identical arrival is consumed", () => {
    const now = vi.spyOn(Date, "now");
    try {
      now.mockReturnValue(1_000_000);
      paramsHolder.current = new URLSearchParams("refreshed=1");
      const consume = vi.fn().mockReturnValue(true);

      // Consume, then leave the page before the strip ever lands.
      const first = renderHook(() =>
        useConsumeSearchParams(consume, { cleanUrl: "/profile" }),
      );
      expect(consume).toHaveBeenCalledTimes(1);
      first.unmount();

      // Well past the remount race window, the same params arrive again.
      now.mockReturnValue(1_000_000 + 11_000);
      renderHook(() => useConsumeSearchParams(consume, { cleanUrl: "/profile" }));

      expect(consume).toHaveBeenCalledTimes(2);
      expect(mockReplace).toHaveBeenCalledTimes(2);
    } finally {
      now.mockRestore();
    }
  });

  it("scopes the remount guard per cleanUrl", () => {
    paramsHolder.current = new URLSearchParams("linked=1");
    const consumeA = vi.fn().mockReturnValue(true);
    const consumeB = vi.fn().mockReturnValue(true);

    renderHook(() => useConsumeSearchParams(consumeA, { cleanUrl: "/a" }));
    renderHook(() => useConsumeSearchParams(consumeB, { cleanUrl: "/b" }));

    expect(consumeA).toHaveBeenCalledTimes(1);
    expect(consumeB).toHaveBeenCalledTimes(1);
  });
});
