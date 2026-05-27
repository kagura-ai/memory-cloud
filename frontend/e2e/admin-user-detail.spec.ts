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
 * Navigate to the first admin user's detail page and wait for the
 * Workspace Capacity section to render. The list is guaranteed non-empty
 * because the test admin themselves is in it. Returns the targeted user_id
 * for tests that need it for further assertions.
 *
 * `waitFor` with an explicit 15s timeout (rather than relying on the
 * follow-on `toBeVisible` auto-wait) is the gate that absorbs slow
 * `next dev` first-paint — `networkidle` is unreliable against `next dev`
 * because the HMR websocket keeps the network busy indefinitely.
 */
async function gotoFirstUserDetail(page: Page): Promise<string> {
  // Direct API call — must use API_URL (backend), not the Playwright config's
  // baseURL which points at Next.js (:3000). There is no Next.js /api/*
  // rewrite, so a relative path would 404.
  const usersRes = await page.request.get(`${API_URL}/api/v1/admin/users`);
  expect(usersRes.ok()).toBe(true);
  const usersBody = (await usersRes.json()) as {
    users: Array<{ id: string }>;
  };
  expect(usersBody.users.length).toBeGreaterThan(0);
  const targetUserId = usersBody.users[0].id;

  await page.goto(`/admin/users/${targetUserId}`, {
    waitUntil: "domcontentloaded",
  });
  await page
    .getByTestId(T.workspaceCapacitySection)
    .waitFor({ state: "visible", timeout: 15_000 });
  return targetUserId;
}

test.describe("/admin/users/[userId] smoke (#688)", () => {
  test("renders the workspace capacity section after admin login", async ({
    page,
  }) => {
    await gotoFirstUserDetail(page);

    await expect(page.getByTestId(T.workspaceCapacityCapDisplay)).toBeVisible();
    await expect(page.getByTestId(T.workspaceCapacityBonusValue)).toBeVisible();
    await expect(page.getByTestId(T.workspaceCapacityIncrement)).toBeVisible();
    await expect(page.getByTestId(T.workspaceCapacityDecrement)).toBeVisible();
  });

  test("[+] then [-] round-trip via real backend (idempotent)", async ({
    page,
  }) => {
    await gotoFirstUserDetail(page);

    const bonusValue = page.getByTestId(T.workspaceCapacityBonusValue);
    const beforeText = await bonusValue.textContent();
    const before = parseInt((beforeText ?? "0").trim(), 10);
    expect(Number.isFinite(before)).toBe(true);

    // [+] → bonus increments by 1. This exercises PATCH /api/v1/admin/users/
    // {user_id}/workspace_slot_bonus end-to-end: real require_admin gate,
    // real DB UPDATE, real audit_log INSERT, real response reconciliation.
    await page.getByTestId(T.workspaceCapacityIncrement).click();
    await expect(bonusValue).toHaveText(String(before + 1));

    // [-] counter-mutation restores the original bonus so this spec is
    // idempotent and leaves no DB residue. The non-destructive decrement
    // path (projectedCap >= owned_count) skips the reason modal and
    // commits immediately.
    await page.getByTestId(T.workspaceCapacityDecrement).click();
    await expect(bonusValue).toHaveText(String(before));
  });
});
