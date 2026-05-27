import type { Page } from "@playwright/test";
import { test, expect, API_URL } from "./fixtures/admin-auth";
import { USER_DETAIL_TEST_IDS as T } from "@/app/(authenticated)/admin/users/[userId]/testids";

/**
 * Admin user detail page E2E smoke (#688).
 *
 * Carves the missing E2E layer for #676's Workspace Capacity feature: URL
 * contract, real RBAC integration (`require_admin` decorator), real
 * audit_log INSERT, and full frontend → backend round-trip via Playwright.
 *
 * Per #688 gate1 (QA Lead): locators are locale-independent (testid only,
 * never visible text) so this spec survives #691's next-intl migration.
 * Testids are imported from a shared `testids.ts` so renames are caught
 * at compile time across page.tsx / page.test.tsx / this spec.
 *
 * Per-issue test coverage map:
 * - #676 unit (Vitest, jsdom)              → covers reducer + reason modal
 * - #676 integration (pytest)              → covers PATCH endpoint contract
 * - #688 E2E (this spec)                   → covers admin auth + URL + UI round-trip
 *
 * The destructive [-] reason-modal path is intentionally NOT covered here
 * because triggering it requires a specific target user state
 * (`projectedCap < owned_count`). That path is fully covered by the
 * Vitest cases in page.test.tsx — duplicating it in E2E adds setup
 * complexity without proportional value.
 */

/**
 * Direct API call — must use API_URL (backend), not the Playwright config's
 * baseURL which points at Next.js (:3000). There is no Next.js /api/*
 * rewrite, so a relative path would 404.
 */
async function listAdminUsers(page: Page): Promise<Array<{ id: string }>> {
  const usersRes = await page.request.get(`${API_URL}/api/v1/admin/users`);
  expect(usersRes.ok()).toBe(true);
  const usersBody = (await usersRes.json()) as { users: Array<{ id: string }> };
  expect(usersBody.users.length).toBeGreaterThan(0);
  return usersBody.users;
}

async function gotoUserDetail(page: Page, userId: string): Promise<void> {
  await page.goto(`/admin/users/${userId}`, {
    waitUntil: "domcontentloaded",
  });
  // Explicit 15s waitFor (rather than relying on follow-on toBeVisible
  // auto-wait) absorbs slow `next dev` first-paint — `networkidle` is
  // unreliable against `next dev` because the HMR websocket keeps the
  // network busy indefinitely.
  await page
    .getByTestId(T.workspaceCapacitySection)
    .waitFor({ state: "visible", timeout: 15_000 });
}

/**
 * Fetch the workspace_summary block for a user via the admin detail API.
 * Returns null if the response shape lacks the block (older API or
 * un-backfilled user).
 */
async function getWorkspaceSummary(
  page: Page,
  userId: string,
): Promise<{
  owned_count: number;
  cap: number;
  base_cap: number;
  workspace_slot_bonus: number;
  is_at_cap: boolean;
} | null> {
  const detailRes = await page.request.get(
    `${API_URL}/api/v1/admin/users/${userId}`,
  );
  if (!detailRes.ok()) return null;
  const detail = (await detailRes.json()) as {
    workspace_summary?: {
      owned_count: number;
      cap: number;
      base_cap: number;
      workspace_slot_bonus: number;
      is_at_cap: boolean;
    } | null;
  };
  return detail.workspace_summary ?? null;
}

test.describe("/admin/users/[userId] smoke (#688)", () => {
  test("renders the workspace capacity section after admin login", async ({
    page,
  }) => {
    const users = await listAdminUsers(page);
    await gotoUserDetail(page, users[0].id);

    await expect(page.getByTestId(T.workspaceCapacityCapDisplay)).toBeVisible();
    await expect(page.getByTestId(T.workspaceCapacityBonusValue)).toBeVisible();
    await expect(page.getByTestId(T.workspaceCapacityIncrement)).toBeVisible();
    await expect(page.getByTestId(T.workspaceCapacityDecrement)).toBeVisible();
  });

  test("[+] then [-] round-trip via real backend (idempotent)", async ({
    page,
  }) => {
    // Round-trip mutation requires a target with safe decrement headroom
    // (cap > owned_count). If the test admin is at-cap or over-cap, the
    // [-] step triggers the destructive reason modal instead of a direct
    // PATCH, leaving the +1 bonus in DB after the test times out.
    // Pick the first non-at-cap candidate via the admin detail API
    // before navigating — surfaced by Copilot review on PR #807.
    const users = await listAdminUsers(page);
    let targetUserId: string | null = null;
    for (const user of users) {
      const summary = await getWorkspaceSummary(page, user.id);
      if (summary && summary.cap > summary.owned_count) {
        targetUserId = user.id;
        break;
      }
    }
    test.skip(
      targetUserId === null,
      "no admin user has cap > owned_count headroom — seed a non-at-cap user before running the round-trip spec",
    );

    await gotoUserDetail(page, targetUserId as string);

    const bonusValue = page.getByTestId(T.workspaceCapacityBonusValue);
    const beforeText = await bonusValue.textContent();
    const before = parseInt((beforeText ?? "0").trim(), 10);
    expect(Number.isFinite(before)).toBe(true);

    // [+] → bonus increments by 1. This exercises PATCH /api/v1/admin/users/
    // {user_id}/workspace_slot_bonus end-to-end: real require_admin gate,
    // real DB UPDATE, real audit_log INSERT, real response reconciliation.
    await page.getByTestId(T.workspaceCapacityIncrement).click();
    await expect(bonusValue).toHaveText(String(before + 1));

    // [-] counter-mutation restores the original bonus. Safe because the
    // pre-test filter above guarantees projectedCap >= owned_count (the
    // non-destructive decrement path).
    await page.getByTestId(T.workspaceCapacityDecrement).click();
    await expect(bonusValue).toHaveText(String(before));
  });
});
