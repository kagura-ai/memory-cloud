/**
 * Stable data-testid values for /admin/users (the list page).
 *
 * Shared between the workspace badge component and its Vitest tests, the
 * same contract `[userId]/testids.ts` keeps for the detail page.
 */
export const WORKSPACE_BADGE_TEST_IDS = {
  badge: "admin-user-workspace-badge",
  overflow: "admin-user-workspace-overflow",
} as const;
