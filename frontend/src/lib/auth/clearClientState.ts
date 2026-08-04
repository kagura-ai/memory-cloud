/**
 * Drop the browser state that belonged to the identity we are leaving
 * (#1488 Phase 4).
 *
 * The session cookie is the server's business; this is the client's half. Once
 * one browser session can carry several accounts, anything cached under a bare
 * key — with no user id in it — silently follows the user across an identity
 * change. `kagura_last_workspace_id` is the one that shows: signed out as A and
 * back in as B, the workspace picker preselects A's workspace.
 *
 * WHAT IS DELIBERATELY KEPT
 *
 * `theme`, `kagura_locale` and `sidebar-collapsed-sections` are DEVICE
 * preferences, not identity: the person set them on this machine, and the
 * common multi-account case is one human with a work and a personal account.
 * Clearing them on every switch would make the app forget how the user likes
 * it every time they move between their own accounts — a worse outcome than
 * the leak it would prevent, since none of the three says anything about who
 * you are.
 *
 * Called on sign-out AND on account switch: both change who the cached data
 * belongs to, so both must clear it.
 */

/** Exact keys written without any user scoping. */
const IDENTITY_SCOPED_KEYS = [
  // Preselects the workspace card, and survives a sign-out (#276).
  "kagura_last_workspace_id",
  // Per-user onboarding progress.
  "onboarding:dismissed",
];

/** Prefixes whose every key is per-user progress. */
const IDENTITY_SCOPED_PREFIXES = ["feature-guide:"];

export function clearIdentityScopedClientState(): void {
  if (typeof window === "undefined") return;

  try {
    for (const key of IDENTITY_SCOPED_KEYS) {
      localStorage.removeItem(key);
    }

    // Collect first, then remove: removeItem() re-indexes localStorage, so
    // deleting inside a forward key(i) walk skips every other match.
    const prefixed: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && IDENTITY_SCOPED_PREFIXES.some((p) => key.startsWith(p))) {
        prefixed.push(key);
      }
    }
    for (const key of prefixed) {
      localStorage.removeItem(key);
    }
  } catch {
    // Storage can be unavailable (private mode, disabled, quota). Failing to
    // tidy the cache must never block the sign-out itself — the server has
    // already ended the session by the time this runs.
  }
}
