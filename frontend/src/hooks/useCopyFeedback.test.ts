/**
 * Tests for useCopyFeedback.
 *
 * Verifies the per-key feedback contract:
 * - Initial: no key is copied
 * - copyToTarget writes to clipboard
 * - isCopied(key) flips true after copy, false after 2s
 * - Multi-target regression: copying key A then key B within 2s leaves
 *   BOTH in the copied state until each key's individual 2s elapses
 *   (the bug fixed by commit 8bc1ee4)
 * - Re-copying the same key restarts only that key's timer
 * - Clipboard errors propagate to the caller (re-thrown)
 * - Unmount clears all pending timers (no setState after unmount)
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useCopyFeedback } from "./useCopyFeedback";

const mockWriteText = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  vi.useFakeTimers();
  mockWriteText.mockReset();
  mockWriteText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: mockWriteText },
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useCopyFeedback", () => {
  it("starts with no key copied", () => {
    const { result } = renderHook(() => useCopyFeedback());
    expect(result.current.isCopied("any-key")).toBe(false);
  });

  it("writes to clipboard and flips isCopied(key) true after copyToTarget", async () => {
    const { result } = renderHook(() => useCopyFeedback());
    await act(async () => {
      await result.current.copyToTarget("hello", "key-a");
    });
    expect(mockWriteText).toHaveBeenCalledWith("hello");
    expect(result.current.isCopied("key-a")).toBe(true);
    expect(result.current.isCopied("key-b")).toBe(false);
  });

  it("clears isCopied(key) after the 2s feedback timer", async () => {
    const { result } = renderHook(() => useCopyFeedback());
    await act(async () => {
      await result.current.copyToTarget("x", "key-a");
    });
    expect(result.current.isCopied("key-a")).toBe(true);

    act(() => {
      vi.advanceTimersByTime(2001);
    });
    expect(result.current.isCopied("key-a")).toBe(false);
  });

  describe("multi-target regression (commit 8bc1ee4 bug fix)", () => {
    it("copying key-b within 2s of key-a leaves BOTH in the copied state", async () => {
      const { result } = renderHook(() => useCopyFeedback());

      // Copy key-a at t=0
      await act(async () => {
        await result.current.copyToTarget("a", "key-a");
      });
      expect(result.current.isCopied("key-a")).toBe(true);

      // Advance 1s, then copy key-b
      await act(async () => {
        vi.advanceTimersByTime(1000);
      });
      await act(async () => {
        await result.current.copyToTarget("b", "key-b");
      });

      // CRITICAL: both keys must show copied. The pre-fix bug cancelled
      // key-a's timer here and only reset key-b's state.
      expect(result.current.isCopied("key-a")).toBe(true);
      expect(result.current.isCopied("key-b")).toBe(true);
    });

    it("each key's timer fires independently on its own schedule", async () => {
      const { result } = renderHook(() => useCopyFeedback());

      // Copy key-a at t=0
      await act(async () => {
        await result.current.copyToTarget("a", "key-a");
      });

      // Copy key-b at t=1000
      await act(async () => {
        vi.advanceTimersByTime(1000);
      });
      await act(async () => {
        await result.current.copyToTarget("b", "key-b");
      });

      // At t=2001 (1s past key-a's reset point), key-a should clear
      // but key-b should still be copied (its timer fires at t=3000).
      await act(async () => {
        vi.advanceTimersByTime(1001);
      });
      expect(result.current.isCopied("key-a")).toBe(false);
      expect(result.current.isCopied("key-b")).toBe(true);

      // Advance to t=3001 — now key-b clears too.
      await act(async () => {
        vi.advanceTimersByTime(1000);
      });
      expect(result.current.isCopied("key-b")).toBe(false);
    });
  });

  it("re-copying the same key restarts only that key's timer", async () => {
    const { result } = renderHook(() => useCopyFeedback());

    await act(async () => {
      await result.current.copyToTarget("a1", "key-a");
    });
    expect(result.current.isCopied("key-a")).toBe(true);

    // Advance 1.5s — partway through the first timer
    await act(async () => {
      vi.advanceTimersByTime(1500);
    });
    expect(result.current.isCopied("key-a")).toBe(true);

    // Re-copy the same key
    await act(async () => {
      await result.current.copyToTarget("a2", "key-a");
    });
    expect(result.current.isCopied("key-a")).toBe(true);

    // Advance another 1.5s (total 3s from first copy, 1.5s from re-copy)
    // The original timer should NOT have fired (it was cancelled by the
    // re-copy). The new timer should still be pending.
    await act(async () => {
      vi.advanceTimersByTime(1500);
    });
    expect(result.current.isCopied("key-a")).toBe(true);

    // Advance another 0.6s (total 2.1s from re-copy) — now the new
    // timer fires.
    await act(async () => {
      vi.advanceTimersByTime(600);
    });
    expect(result.current.isCopied("key-a")).toBe(false);
  });

  it("propagates clipboard.writeText errors to the caller", async () => {
    mockWriteText.mockRejectedValueOnce(new Error("clipboard denied"));
    const { result } = renderHook(() => useCopyFeedback());

    await act(async () => {
      await expect(result.current.copyToTarget("x", "key-a")).rejects.toThrow(
        "clipboard denied",
      );
    });
    // The key should NOT be flagged as copied on failure
    expect(result.current.isCopied("key-a")).toBe(false);
  });

  it("does not fire timers after unmount", async () => {
    const { result, unmount } = renderHook(() => useCopyFeedback());
    await act(async () => {
      await result.current.copyToTarget("x", "key-a");
    });

    unmount();

    // Advancing the clock should not throw (no setState on unmounted)
    expect(() => {
      vi.advanceTimersByTime(2001);
    }).not.toThrow();
  });
});
