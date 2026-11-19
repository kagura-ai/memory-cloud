/**
 * Workspace management (#2919): where an owner manages owned workspaces,
 * including suspended ones, and the once-per-session prompt that sends an
 * owner there when a choice is still needed.
 */

/** The in-app path of Account settings > Workspace management. */
export const WORKSPACE_MANAGEMENT_PATH = "/profile/workspaces";

const PROMPT_KEY_PREFIX = "kagura_workspace_selection_prompted:";

/** Users prompted in this page load, for when sessionStorage is unavailable. */
const promptedWithoutStorage = new Set<string>();

/**
 * Take this browser session's one prompt for `userId`: true the first time,
 * false afterwards. Kept per user in sessionStorage, so a second account
 * signed in in the same tab is prompted too. Storage that throws (private
 * mode, blocked site data) falls back to this page load.
 */
export function takeSelectionPrompt(userId: string): boolean {
  const key = `${PROMPT_KEY_PREFIX}${userId}`;
  try {
    if (window.sessionStorage.getItem(key) === "1") return false;
    window.sessionStorage.setItem(key, "1");
    return true;
  } catch {
    if (promptedWithoutStorage.has(userId)) return false;
    promptedWithoutStorage.add(userId);
    return true;
  }
}

/**
 * Forget every prompt of this tab (sign-out, account switch), so the next
 * sign-in in the same tab is prompted again.
 */
export function clearSelectionPrompts(): void {
  promptedWithoutStorage.clear();
  try {
    // Collect first, then remove: removeItem() re-indexes the storage.
    const keys: string[] = [];
    for (let i = 0; i < window.sessionStorage.length; i++) {
      const key = window.sessionStorage.key(i);
      if (key?.startsWith(PROMPT_KEY_PREFIX)) keys.push(key);
    }
    for (const key of keys) window.sessionStorage.removeItem(key);
  } catch {
    // Unavailable storage holds no prompt to forget.
  }
}

/** Tests only: forget the page-load fallback. */
export function resetSelectionPromptFallback(): void {
  promptedWithoutStorage.clear();
}
