/**
 * Tests for useAutoOpenOnFreshWindow.
 *
 * Verifies the auto-open / manual-close semantics:
 * - Initial null → closed
 * - null → string transition → opens
 * - Same string twice → opens once (auto-refresh polling does not reopen)
 * - User close → next render with same string → stays closed
 * - Different string after close → reopens (fresh window respected)
 * - string → null → no change
 */

import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useAutoOpenOnFreshWindow } from "./useAutoOpenOnFreshWindow";

describe("useAutoOpenOnFreshWindow", () => {
  it("starts closed when initial value is null", () => {
    const { result } = renderHook(() => useAutoOpenOnFreshWindow(null));
    expect(result.current[0]).toBe(false);
  });

  it("opens on initial render with a non-null timestamp", () => {
    const { result } = renderHook(() =>
      useAutoOpenOnFreshWindow("2026-04-11T10:00:00Z"),
    );
    expect(result.current[0]).toBe(true);
  });

  it("opens when transitioning from null to a non-null timestamp", () => {
    const { result, rerender } = renderHook(
      ({ ts }: { ts: string | null }) => useAutoOpenOnFreshWindow(ts),
      { initialProps: { ts: null as string | null } },
    );
    expect(result.current[0]).toBe(false);

    rerender({ ts: "2026-04-11T10:00:00Z" });
    expect(result.current[0]).toBe(true);
  });

  it("does NOT reopen when the same timestamp arrives twice (e.g. auto-refresh poll)", () => {
    const { result, rerender } = renderHook(
      ({ ts }: { ts: string | null }) => useAutoOpenOnFreshWindow(ts),
      { initialProps: { ts: "2026-04-11T10:00:00Z" as string | null } },
    );
    expect(result.current[0]).toBe(true);

    // User closes manually
    act(() => {
      result.current[1](false);
    });
    expect(result.current[0]).toBe(false);

    // Same timestamp on next render — must respect the user close
    rerender({ ts: "2026-04-11T10:00:00Z" });
    expect(result.current[0]).toBe(false);
  });

  it("reopens when a NEW timestamp arrives after a user close (fresh window)", () => {
    const { result, rerender } = renderHook(
      ({ ts }: { ts: string | null }) => useAutoOpenOnFreshWindow(ts),
      { initialProps: { ts: "2026-04-11T10:00:00Z" as string | null } },
    );
    expect(result.current[0]).toBe(true);

    // User closes
    act(() => {
      result.current[1](false);
    });
    expect(result.current[0]).toBe(false);

    // A new timestamp (e.g. user regenerated the key) → reopens
    rerender({ ts: "2026-04-11T10:30:00Z" });
    expect(result.current[0]).toBe(true);
  });

  it("does NOT change state when transitioning from a timestamp to null", () => {
    const { result, rerender } = renderHook(
      ({ ts }: { ts: string | null }) => useAutoOpenOnFreshWindow(ts),
      { initialProps: { ts: "2026-04-11T10:00:00Z" as string | null } },
    );
    expect(result.current[0]).toBe(true);

    // Visibility window expires → ts becomes null
    rerender({ ts: null });
    // The hook does not auto-close on transition to null — closing is the
    // user's job (or the parent component's via setIsOpen).
    expect(result.current[0]).toBe(true);
  });

  it("respects explicit setIsOpen(true) calls without a timestamp change", () => {
    const { result } = renderHook(() => useAutoOpenOnFreshWindow(null));
    expect(result.current[0]).toBe(false);

    act(() => {
      result.current[1](true);
    });
    expect(result.current[0]).toBe(true);
  });
});
