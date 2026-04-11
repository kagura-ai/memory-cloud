"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Default duration to show the "copied" feedback affordance after a
 * successful copy. Independent from any clipboard auto-clear (this is
 * purely for the icon swap on the button).
 */
const COPIED_FEEDBACK_MS = 2000;

export interface UseCopyFeedbackReturn {
  /**
   * True for `COPIED_FEEDBACK_MS` after a successful `copyToTarget` call
   * for the matching `key`. Each key has its own independent timer, so
   * multiple buttons can show the "copied" check simultaneously.
   */
  isCopied: (key: string) => boolean;
  /**
   * Write `text` to the clipboard and flag `key` as recently copied.
   * Cancels any pending feedback timer for THIS key only — other keys'
   * timers are unaffected, so multi-target copy sequences do not stomp
   * each other.
   *
   * Re-throws clipboard errors so the caller can fire a destructive toast
   * via the existing 3-channel error rule (per .claude/rules/frontend.md).
   */
  copyToTarget: (text: string, key: string) => Promise<void>;
}

/**
 * Manage per-key "copied" feedback state for a panel that has multiple
 * copy buttons (each identified by a string key). The previous shared-ref
 * pattern in APIKeysTabPanel and OAuthAppsTabPanel had a bug where copying
 * a second target within `COPIED_FEEDBACK_MS` would cancel the first
 * target's reset timer, leaving the first button stuck in the "copied"
 * state. This hook fixes that by giving each key its own timer slot in
 * a Record.
 *
 * On unmount, all pending timers are cleared. The cleanup function reads
 * `copyTimeoutsRef.current` directly (not via a captured local) so future
 * refactors that reassign the ref's `.current` would still work — the
 * pattern is robust to both mutation-in-place AND reassignment styles.
 *
 * @example
 * ```tsx
 * const { isCopied, copyToTarget } = useCopyFeedback();
 *
 * const handleCopy = async (text: string, key: string) => {
 *   try {
 *     await copyToTarget(text, key);
 *   } catch (err) {
 *     toast({ title: tCommon("error"), description: ..., variant: "destructive" });
 *   }
 * };
 *
 * <Button onClick={() => handleCopy(mcpUrl, "mcp-url")}>
 *   {isCopied("mcp-url") ? <Check /> : <Copy />}
 * </Button>
 * ```
 */
export function useCopyFeedback(): UseCopyFeedbackReturn {
  const [copiedItems, setCopiedItems] = useState<Record<string, boolean>>({});
  const isMountedRef = useRef(true);
  const copyTimeoutsRef = useRef<Record<string, ReturnType<typeof setTimeout>>>(
    {},
  );

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      // Read the ref directly at cleanup time so we observe ALL pending
      // timers, including those added between mount and unmount. This is
      // robust to both mutation-in-place AND a future refactor that
      // reassigns `copyTimeoutsRef.current` to a new object.
      for (const t of Object.values(copyTimeoutsRef.current)) {
        clearTimeout(t);
      }
    };
  }, []);

  const isCopied = useCallback(
    (key: string) => copiedItems[key] === true,
    [copiedItems],
  );

  const copyToTarget = useCallback(
    async (text: string, key: string): Promise<void> => {
      // Re-throws on failure so the caller can fire a destructive toast.
      // We do not attempt to recover here — the caller's error handler
      // owns the user-facing surface (toast vs inline message vs banner).
      await navigator.clipboard.writeText(text);

      if (!isMountedRef.current) return;

      setCopiedItems((prev) => ({ ...prev, [key]: true }));

      // Cancel only THIS key's pending timer — leave other keys alone.
      // The previous bug (single shared ref) cancelled all keys' timers
      // here and only reset the latest one's state.
      const existing = copyTimeoutsRef.current[key];
      if (existing) {
        clearTimeout(existing);
      }

      copyTimeoutsRef.current[key] = setTimeout(() => {
        if (isMountedRef.current) {
          // DELETE the key from state instead of setting it to false.
          // Setting `false` would leave the key in the Record forever,
          // which causes unbounded growth in panels with many distinct
          // copy targets (e.g. lists with dynamic IDs). The Record now
          // shrinks back to empty when no copy is "fresh".
          setCopiedItems((prev) => {
            if (!(key in prev)) return prev;
            const { [key]: _removed, ...rest } = prev;
            return rest;
          });
        }
        delete copyTimeoutsRef.current[key];
      }, COPIED_FEEDBACK_MS);
    },
    [],
  );

  return { isCopied, copyToTarget };
}
