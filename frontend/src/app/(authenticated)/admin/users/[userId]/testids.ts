/**
 * Stable data-testid values for /admin/users/[userId].
 *
 * Shared contract between:
 * - the page itself (page.tsx)
 * - Vitest unit tests (page.test.tsx)
 * - Playwright E2E (frontend/e2e/admin-user-detail.spec.ts, #688)
 *
 * Centralizing here removes the rename foot-gun: TypeScript catches drift
 * across all three consumers instead of relying on a comment to coordinate.
 */
export const USER_DETAIL_TEST_IDS = {
  workspaceCapacitySection: "workspace-capacity-section",
  workspaceCapacityCapDisplay: "workspace-capacity-cap-display",
  workspaceCapacityBonusValue: "workspace-capacity-bonus-value",
  workspaceCapacityIncrement: "workspace-capacity-increment",
  workspaceCapacityDecrement: "workspace-capacity-decrement",
  reasonModal: "reason-modal",
  reasonModalInput: "reason-modal-input",
  reasonModalConfirm: "reason-modal-confirm",
} as const;
