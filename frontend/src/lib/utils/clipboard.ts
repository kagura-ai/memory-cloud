/**
 * Clipboard copy utility with graceful degradation (issue #987).
 *
 * The async Clipboard API (`navigator.clipboard.writeText`) is the preferred
 * path, but it requires a secure context AND a focused document. When the
 * browser denies the write it throws `NotAllowedError: Failed to execute
 * 'writeText' on 'Clipboard': Write permission denied.` — which happens when
 * DevTools is the focused surface, the tab is backgrounded, the origin is an
 * insecure non-localhost `http://`, or inside hardened/embedded webviews.
 *
 * Previously both copy hooks called `navigator.clipboard.writeText` directly
 * with no fallback, so any denial surfaced the raw DOM exception in a toast
 * and left the user with no way to copy — a dead end for one-time-reveal API
 * keys. `copyText` adds a legacy `document.execCommand('copy')` fallback and a
 * typed error so callers can render an actionable, i18n'd message.
 */

/**
 * Thrown when `copyText` exhausts both the async Clipboard API and the legacy
 * `execCommand` fallback (or runs where neither exists, e.g. SSR). Callers in
 * a user-action context catch this and surface a friendly, i18n'd message;
 * best-effort callers (clipboard auto-clear) catch and ignore it.
 */
export class ClipboardCopyError extends Error {
  /** The underlying error from the primary (async clipboard) attempt, if any. */
  readonly cause?: unknown;

  constructor(message = "clipboard_copy_failed", cause?: unknown) {
    super(message);
    this.name = "ClipboardCopyError";
    this.cause = cause;
  }
}

/**
 * Copy `text` to the clipboard, degrading gracefully across environments.
 *
 * Attempt order:
 * 1. `navigator.clipboard.writeText` — preferred (secure context + focus).
 * 2. `document.execCommand('copy')` via a hidden, off-screen `<textarea>` —
 *    covers denial, insecure origins, and embedded webviews.
 *
 * Resolves on the first success. Throws {@link ClipboardCopyError} only when
 * every available mechanism fails. The fallback textarea transiently holds
 * `text` (which may be a plaintext secret), so it is removed synchronously in
 * a `finally` block and prior focus/selection is restored.
 *
 * @param text - the string to place on the clipboard.
 * @throws ClipboardCopyError when no mechanism succeeds.
 */
export async function copyText(text: string): Promise<void> {
  // SSR / non-DOM guard: there is no clipboard or document to copy into.
  if (typeof document === "undefined") {
    throw new ClipboardCopyError("clipboard_unavailable");
  }

  let primaryError: unknown;

  // 1. Async Clipboard API. Access via a local so a throwing property getter
  //    (some hardened environments) degrades to the fallback instead of
  //    crashing.
  let clipboard: Clipboard | undefined;
  try {
    clipboard = typeof navigator !== "undefined" ? navigator.clipboard : undefined;
  } catch {
    clipboard = undefined;
  }

  if (clipboard && typeof clipboard.writeText === "function") {
    try {
      await clipboard.writeText(text);
      return;
    } catch (err) {
      // Remember the cause, then fall through to the legacy fallback.
      primaryError = err;
    }
  }

  // 2. Legacy execCommand fallback.
  if (legacyExecCommandCopy(text)) {
    return;
  }

  throw new ClipboardCopyError("clipboard_copy_failed", primaryError);
}

/**
 * Synchronous legacy copy via a hidden textarea + `document.execCommand('copy')`.
 * Returns `true` on success, `false` otherwise. Never throws — any DOM
 * exception is treated as "fallback unavailable". Restores prior focus so the
 * user's caret position is not disturbed.
 */
function legacyExecCommandCopy(text: string): boolean {
  if (typeof document.execCommand !== "function") {
    return false;
  }

  const previouslyFocused = document.activeElement as HTMLElement | null;
  const textarea = document.createElement("textarea");

  try {
    textarea.value = text;
    // Keep it out of view and out of layout/scroll, but still selectable.
    textarea.setAttribute("readonly", "");
    textarea.setAttribute("aria-hidden", "true");
    textarea.tabIndex = -1;
    textarea.style.position = "fixed";
    textarea.style.top = "-9999px";
    textarea.style.left = "-9999px";
    textarea.style.opacity = "0";

    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();

    return document.execCommand("copy") === true;
  } catch {
    return false;
  } finally {
    // Synchronous teardown — the textarea holds the (possibly secret) value
    // and must never linger in the DOM. Restore prior focus afterwards.
    textarea.remove();
    if (previouslyFocused && typeof previouslyFocused.focus === "function") {
      previouslyFocused.focus();
    }
  }
}
