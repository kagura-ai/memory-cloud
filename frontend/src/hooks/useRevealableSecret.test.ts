/**
 * Tests for useRevealableSecret.
 *
 * Verifies:
 * - reveal/hide/toggle state transitions
 * - clipboard.writeText is called on copy
 * - "copied" feedback flag is set then cleared after 2s
 * - clipboard auto-clear fires after autoClearMs
 * - re-copy cancels the previous auto-clear timer
 * - unmount cleans up all timers
 * - autoClearMs=0 disables auto-clear
 * - clipboard.writeText errors propagate to caller
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useRevealableSecret } from "./useRevealableSecret";

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

describe("useRevealableSecret", () => {
  describe("reveal state", () => {
    it("starts in masked state", () => {
      const { result } = renderHook(() => useRevealableSecret());
      expect(result.current.revealed).toBe(false);
    });

    it("show() sets revealed to true", () => {
      const { result } = renderHook(() => useRevealableSecret());
      act(() => {
        result.current.show();
      });
      expect(result.current.revealed).toBe(true);
    });

    it("hide() sets revealed to false", () => {
      const { result } = renderHook(() => useRevealableSecret());
      act(() => {
        result.current.show();
      });
      act(() => {
        result.current.hide();
      });
      expect(result.current.revealed).toBe(false);
    });

    it("toggle() flips revealed state", () => {
      const { result } = renderHook(() => useRevealableSecret());
      act(() => {
        result.current.toggle();
      });
      expect(result.current.revealed).toBe(true);
      act(() => {
        result.current.toggle();
      });
      expect(result.current.revealed).toBe(false);
    });
  });

  describe("copy action", () => {
    it("calls navigator.clipboard.writeText with the given text", async () => {
      const { result } = renderHook(() => useRevealableSecret());
      await act(async () => {
        await result.current.copy("secret-value");
      });
      expect(mockWriteText).toHaveBeenCalledWith("secret-value");
    });

    it("sets copied=true after a successful copy", async () => {
      const { result } = renderHook(() => useRevealableSecret());
      await act(async () => {
        await result.current.copy("x");
      });
      expect(result.current.copied).toBe(true);
    });

    it("clears copied=false after 2 seconds", async () => {
      const { result } = renderHook(() => useRevealableSecret());
      await act(async () => {
        await result.current.copy("x");
      });
      expect(result.current.copied).toBe(true);
      act(() => {
        vi.advanceTimersByTime(2001);
      });
      expect(result.current.copied).toBe(false);
    });

    it("propagates clipboard.writeText errors", async () => {
      mockWriteText.mockRejectedValueOnce(new Error("clipboard denied"));
      const { result } = renderHook(() => useRevealableSecret());
      await act(async () => {
        await expect(result.current.copy("x")).rejects.toThrow(
          "clipboard denied",
        );
      });
    });

    it("preserves previous copy's copied state when a re-copy within the feedback window fails", async () => {
      const { result } = renderHook(() => useRevealableSecret());

      // First successful copy at t=0
      await act(async () => {
        await result.current.copy("first");
      });
      expect(result.current.copied).toBe(true);

      // 1s later (within 2s feedback window), attempt a re-copy that fails
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      mockWriteText.mockRejectedValueOnce(new Error("denied"));
      await act(async () => {
        await expect(result.current.copy("second")).rejects.toThrow("denied");
      });

      // CRITICAL: copied must STILL be true — the first copy's feedback
      // timer is intact, not cancelled. Pre-fix bug: cancellation happened
      // BEFORE the failed write, leaving copied stuck true forever.
      expect(result.current.copied).toBe(true);

      // Original feedback timer should still fire at t=2.001s (1s more)
      act(() => {
        vi.advanceTimersByTime(1001);
      });
      expect(result.current.copied).toBe(false);
    });

    it("preserves previous copy's auto-clear timer when a re-copy fails", async () => {
      const { result } = renderHook(() => useRevealableSecret());

      // First successful copy at t=0
      await act(async () => {
        await result.current.copy("first");
      });
      expect(mockWriteText).toHaveBeenCalledTimes(1);

      // 30s later, attempt a re-copy that fails
      act(() => {
        vi.advanceTimersByTime(30_000);
      });
      mockWriteText.mockRejectedValueOnce(new Error("denied"));
      await act(async () => {
        await expect(result.current.copy("second")).rejects.toThrow("denied");
      });
      // 2 calls: "first" (success) + "second" (rejected). NO timer cancellation.
      expect(mockWriteText).toHaveBeenCalledTimes(2);

      // Wait for the ORIGINAL 60s auto-clear (30s more from now)
      await act(async () => {
        vi.advanceTimersByTime(30_001);
        await Promise.resolve();
      });

      // CRITICAL: the original auto-clear must still fire. Pre-fix bug:
      // the auto-clear was cancelled at the top of the failed re-copy,
      // leaving "first" in the clipboard past the 60s window.
      expect(mockWriteText).toHaveBeenCalledTimes(3);
      expect(mockWriteText).toHaveBeenLastCalledWith("");
    });
  });

  describe("clipboard auto-clear", () => {
    it("clears the clipboard after autoClearMs (default 60s)", async () => {
      const { result } = renderHook(() => useRevealableSecret());
      await act(async () => {
        await result.current.copy("secret");
      });
      expect(mockWriteText).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(60_001);
        // flush microtasks for the .then in the auto-clear chain
        await Promise.resolve();
      });

      // 2nd call should be the empty-string clear
      expect(mockWriteText).toHaveBeenCalledTimes(2);
      expect(mockWriteText).toHaveBeenLastCalledWith("");
      expect(result.current.clipboardCleared).toBe(true);
    });

    it("respects custom autoClearMs", async () => {
      const { result } = renderHook(() =>
        useRevealableSecret({ autoClearMs: 5_000 }),
      );
      await act(async () => {
        await result.current.copy("secret");
      });

      await act(async () => {
        vi.advanceTimersByTime(5_001);
        await Promise.resolve();
      });

      expect(mockWriteText).toHaveBeenLastCalledWith("");
    });

    it("does NOT auto-clear when autoClearMs=0", async () => {
      const { result } = renderHook(() =>
        useRevealableSecret({ autoClearMs: 0 }),
      );
      await act(async () => {
        await result.current.copy("secret");
      });
      expect(mockWriteText).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(120_000);
        await Promise.resolve();
      });

      // Still only the original copy
      expect(mockWriteText).toHaveBeenCalledTimes(1);
      expect(result.current.clipboardCleared).toBe(false);
    });

    it("re-copy cancels the previous auto-clear timer", async () => {
      const { result } = renderHook(() => useRevealableSecret());

      await act(async () => {
        await result.current.copy("first");
      });

      // Advance partway, then re-copy
      act(() => {
        vi.advanceTimersByTime(30_000);
      });
      await act(async () => {
        await result.current.copy("second");
      });

      // Advance past the ORIGINAL 60s mark — the first auto-clear should NOT fire
      await act(async () => {
        vi.advanceTimersByTime(40_000);
        await Promise.resolve();
      });
      // Only the two real copies so far (no clear yet from the second timer)
      expect(mockWriteText).toHaveBeenCalledTimes(2);

      // Advance to complete the SECOND timer (started at t=30s, fires at t=90s)
      await act(async () => {
        vi.advanceTimersByTime(20_001);
        await Promise.resolve();
      });
      // Now the auto-clear from the second copy should have fired
      expect(mockWriteText).toHaveBeenCalledTimes(3);
      expect(mockWriteText).toHaveBeenLastCalledWith("");
    });

    it("marks clipboardCleared=true even if the clear write fails", async () => {
      const { result } = renderHook(() => useRevealableSecret());
      await act(async () => {
        await result.current.copy("secret");
      });

      // Make the next writeText (the clear) fail
      mockWriteText.mockRejectedValueOnce(new Error("clipboard denied"));

      await act(async () => {
        vi.advanceTimersByTime(60_001);
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(result.current.clipboardCleared).toBe(true);
    });

    it("does not crash when navigator.clipboard is missing at clear time", async () => {
      const { result } = renderHook(() => useRevealableSecret());
      await act(async () => {
        await result.current.copy("secret");
      });

      // Simulate an environment where the clipboard API disappears between
      // the user-initiated copy and the auto-clear timer firing (e.g.
      // hardened security context). The clear must NOT crash the app.
      Object.defineProperty(navigator, "clipboard", {
        value: undefined,
        writable: true,
        configurable: true,
      });

      expect(() => {
        act(() => {
          vi.advanceTimersByTime(60_001);
        });
      }).not.toThrow();

      expect(result.current.clipboardCleared).toBe(true);
    });

    it("does not crash when accessing navigator.clipboard throws synchronously", async () => {
      const { result } = renderHook(() => useRevealableSecret());
      await act(async () => {
        await result.current.copy("secret");
      });

      // Simulate a property accessor that throws (some hardened environments
      // do this when clipboard access is denied at the policy layer).
      Object.defineProperty(navigator, "clipboard", {
        get() {
          throw new Error("clipboard access denied");
        },
        configurable: true,
      });

      expect(() => {
        act(() => {
          vi.advanceTimersByTime(60_001);
        });
      }).not.toThrow();

      expect(result.current.clipboardCleared).toBe(true);
    });
  });

  describe("unmount cleanup", () => {
    it("does not fire timers after unmount", async () => {
      const { result, unmount } = renderHook(() => useRevealableSecret());
      await act(async () => {
        await result.current.copy("secret");
      });
      expect(mockWriteText).toHaveBeenCalledTimes(1);

      unmount();

      act(() => {
        vi.advanceTimersByTime(60_001);
      });
      // No additional calls — auto-clear should NOT have fired
      expect(mockWriteText).toHaveBeenCalledTimes(1);
    });
  });
});
