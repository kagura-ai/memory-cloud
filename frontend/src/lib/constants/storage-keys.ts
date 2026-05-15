/**
 * Shared localStorage keys.
 *
 * Cross-file keys live here so a one-character typo in either reader or
 * writer surfaces as a type error rather than a silent miss.
 */

// Issue #660: stashed by /workspace/settings/general before delete, consumed
// by /workspace/dashboard on next mount to render the auto-switch toast.
export const RECENTLY_DELETED_WORKSPACE_KEY =
  "kagura_recently_deleted_workspace";
