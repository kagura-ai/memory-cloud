"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { copyText } from "@/lib/utils/clipboard";

/**
 * Default duration to keep the "copied" UI affordance visible after a copy.
 * Independent from clipboard auto-clear — this is purely for the icon swap
 * (Copy → Check → Copy) on the button.
 */
const COPIED_FEEDBACK_MS = 2000;

/**
 * Default clipboard auto-clear duration after copying a secret.
 * Chosen as 60s based on a DX assessment of the zero-to-hello-world flow:
 * the user must switch apps, open .mcp.json (or equivalent), and paste —
 * 30s is too tight, 120s is too lax. Tests can override via
 * `autoClearMs` prop. Set to 0 to disable auto-clear entirely.
 */
const DEFAULT_AUTO_CLEAR_MS = 60_000;

export interface UseRevealableSecretOptions {
  /**
   * Duration in ms after which the clipboard is auto-cleared by writing
   * an empty string. Defaults to 60_000 (60 seconds). Set to 0 to disable.
   *
   * Note: clipboard auto-clear is best-effort. Some browsers and OSes do
   * not allow programmatic clipboard writes after the user-initiated
   * gesture window. The hook silently swallows any failure from the clear
   * call so the consumer never sees an error from defense-in-depth code.
   */
  autoClearMs?: number;
}

export interface UseRevealableSecretReturn {
  /** True when the secret should be visually shown to the user. */
  revealed: boolean;
  /** Force the secret into revealed state. */
  show: () => void;
  /** Force the secret into masked state. */
  hide: () => void;
  /** Toggle revealed state. */
  toggle: () => void;
  /**
   * Write the given text to the clipboard. Returns the underlying promise
   * so the consumer can await it. Resets the auto-clear timer if a previous
   * copy is still pending.
   */
  copy: (text: string) => Promise<void>;
  /**
   * True for COPIED_FEEDBACK_MS after a successful copy. Useful for swapping
   * a Copy icon to a Check icon as visual confirmation.
   */
  copied: boolean;
  /**
   * True after the auto-clear timer has fired and a clear was attempted.
   * Best-effort signal — does not guarantee the clipboard is actually empty
   * (browsers may reject the clear).
   */
  clipboardCleared: boolean;
}

/**
 * Manage the state and timers for a "reveal-on-demand" secret display with
 * optional clipboard auto-clear after copy.
 *
 * Used by:
 * - `MaskedSecretField` for standalone secret display blocks
 * - `MCPConfigBlock` for the JSON snippet with embedded API key
 *
 * The hook owns: revealed state, copy action, ephemeral "copied" feedback,
 * clipboard auto-clear timer, and unmount cleanup. Consumers own the visual
 * rendering of the secret.
 *
 * On unmount, all pending timers are cleared. If a re-copy happens before the
 * previous auto-clear timer fires, the previous timer is canceled and a new
 * one is started — clipboard contains only the latest value, and only the
 * latest auto-clear can fire.
 */
export function useRevealableSecret(
  options?: UseRevealableSecretOptions,
): UseRevealableSecretReturn {
  const autoClearMs = options?.autoClearMs ?? DEFAULT_AUTO_CLEAR_MS;

  const [revealed, setRevealed] = useState(false);
  const [copied, setCopied] = useState(false);
  const [clipboardCleared, setClipboardCleared] = useState(false);

  const copiedFeedbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const autoClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isMountedRef = useRef(true);

  // Cleanup all timers on unmount.
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      if (copiedFeedbackTimerRef.current) {
        clearTimeout(copiedFeedbackTimerRef.current);
      }
      if (autoClearTimerRef.current) {
        clearTimeout(autoClearTimerRef.current);
      }
    };
  }, []);

  const show = useCallback(() => setRevealed(true), []);
  const hide = useCallback(() => setRevealed(false), []);
  const toggle = useCallback(() => setRevealed((v) => !v), []);

  const copy = useCallback(
    async (text: string): Promise<void> => {
      // Attempt the clipboard write FIRST, before touching any timer or
      // state. If the write fails, the previous copy's lifecycle stays
      // intact: the prior secret's auto-clear still fires on schedule,
      // and the prior secret's "copied" feedback timer still resets the
      // icon. A failed re-copy must NOT strand the previous secret in
      // the clipboard with no auto-clear pending, and must NOT leave the
      // copied state stuck true with no feedback timer to reset it.
      //
      // Re-throws on failure so the caller can show a destructive toast
      // (clipboard write failure is a user-visible action failure, not
      // a defense-in-depth background event). copyText degrades to an
      // execCommand fallback before throwing (issue #987), so a denied
      // async-clipboard write no longer dead-ends one-time-reveal secrets.
      await copyText(text);

      if (!isMountedRef.current) return;

      // Write succeeded — now it's safe to cancel the previous copy's
      // timers (the new state will replace the old) and start fresh ones
      // for this copy.
      if (copiedFeedbackTimerRef.current) {
        clearTimeout(copiedFeedbackTimerRef.current);
        copiedFeedbackTimerRef.current = null;
      }
      if (autoClearTimerRef.current) {
        clearTimeout(autoClearTimerRef.current);
        autoClearTimerRef.current = null;
      }

      setCopied(true);
      setClipboardCleared(false);

      // "Copied" UI feedback timer (icon swap).
      copiedFeedbackTimerRef.current = setTimeout(() => {
        if (isMountedRef.current) {
          setCopied(false);
        }
      }, COPIED_FEEDBACK_MS);

      // Clipboard auto-clear timer (defense in depth). Skip if disabled.
      if (autoClearMs > 0) {
        autoClearTimerRef.current = setTimeout(() => {
          if (!isMountedRef.current) return;
          // Best-effort: silently swallow errors. Browser may reject the
          // write because we are outside the user-initiated gesture window,
          // AND some environments (older browsers, hardened security
          // contexts, embedded webviews) may not expose `navigator.clipboard`
          // at all or may throw SYNCHRONOUSLY when the property is accessed.
          // Wrap the access in try/catch so a missing clipboard API never
          // crashes the app — defense-in-depth code must not break the
          // primary user flow.
          try {
            const clipboard = navigator.clipboard;
            const writeText = clipboard?.writeText;
            if (typeof writeText !== "function") {
              if (isMountedRef.current) {
                setClipboardCleared(true);
              }
              return;
            }
            writeText
              .call(clipboard, "")
              .then(() => {
                if (isMountedRef.current) {
                  setClipboardCleared(true);
                }
              })
              .catch(() => {
                // Even on failure, mark as "attempted" so consumers can stop
                // showing the "will clear in N seconds" hint.
                if (isMountedRef.current) {
                  setClipboardCleared(true);
                }
              });
          } catch {
            if (isMountedRef.current) {
              setClipboardCleared(true);
            }
          }
        }, autoClearMs);
      }
    },
    [autoClearMs],
  );

  return {
    revealed,
    show,
    hide,
    toggle,
    copy,
    copied,
    clipboardCleared,
  };
}
