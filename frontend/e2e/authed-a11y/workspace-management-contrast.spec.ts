import path from "node:path";
import type { Page } from "@playwright/test";
import {
  API_URL,
  SLOT_OWNER_LOGIN_ID,
  expect,
  test,
} from "../fixtures/admin-auth";
import {
  assertNoColorContrastViolations,
  gotoAndWaitStable,
} from "../fixtures";
import en from "@/messages/en.json";

/**
 * Account settings > Workspace management (#2919), the issue's UI check.
 *
 * NON-HERMETIC: needs a live backend with `WORKSPACE_SLOT_SUSPENSION=enforce`
 * and the slot owner seeded by `seed_e2e_admin --slot-owner`: a separate
 * password user owning three workspaces with one slot, two of them suspended,
 * and a choice still to make. Auth is the `slot-owner.json` storageState
 * written by `auth.setup.ts`; the whole file skips where
 * `E2E_SLOT_OWNER_LOGIN_ID` is unset.
 *
 * Serial: the last test gives the owner two bonus slots through the admin API
 * (the "return to active" half of the check), which changes the state every
 * earlier test reads. sessionStorage is not part of a storageState, so each
 * test starts a fresh browser session (the redirect fires in each).
 */

const AUTH_DIR = path.join(__dirname, "..", ".auth");
const SLOT_OWNER_STATE = path.join(AUTH_DIR, "slot-owner.json");
const ADMIN_STATE = path.join(AUTH_DIR, "admin.json");

const T = en.workspaceSlots;
const ROWS = '[data-testid^="slot-row-"]';
const fill = (message: string, values: Record<string, string | number>) =>
  Object.entries(values).reduce(
    (text, [key, value]) => text.replaceAll(`{${key}}`, String(value)),
    message,
  );

test.skip(
  !SLOT_OWNER_LOGIN_ID,
  "E2E_SLOT_OWNER_LOGIN_ID is unset: no slot-owner seed in this lane",
);
test.describe.configure({ mode: "serial" });
test.use({ storageState: SLOT_OWNER_STATE, locale: "en-US" });

async function openPage(page: Page) {
  await gotoAndWaitStable(page, "/profile/workspaces", ROWS);
  await expect(page.locator("h1")).toHaveText(T.title);
}

test.describe("/profile/workspaces (#2919)", () => {
  test("an owner who must choose lands on Workspace management", async ({
    page,
  }) => {
    await page.goto("/workspace/dashboard", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/profile\/workspaces(\?|$)/, {
      timeout: 15_000,
    });
    await expect(page.locator(ROWS).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator("h1")).toHaveText(T.title);
    await expect(
      page.getByText(T.selectionRequired.title, { exact: true }),
    ).toBeVisible();
  });

  test("shows 1 of 1 active, 3 owned, and two suspended rows", async ({
    page,
  }) => {
    await openPage(page);
    await expect(
      page.getByText(fill(T.summary.active, { active: 1, slots: 1 }), {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByText(fill(T.summary.owned, { count: 3 }), { exact: true }),
    ).toBeVisible();
    await expect(page.locator(ROWS)).toHaveCount(3);
    await expect(
      page
        .locator(ROWS)
        .filter({ has: page.getByText(T.status.slot_suspended, { exact: true }) }),
    ).toHaveCount(2);
  });

  for (const colorScheme of ["light", "dark"] as const) {
    test(`${colorScheme} mode has no color-contrast violations`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await openPage(page);
      await assertNoColorContrastViolations(page, ROWS);
    });
  }

  test("keyboard: choose another workspace and preview it without saving", async ({
    page,
  }) => {
    const saves: string[] = [];
    page.on("request", (request) => {
      if (
        request.method() === "PUT" &&
        request.url().includes("/api/v1/me/workspace-slots/selection")
      ) {
        saves.push(request.url());
      }
    });
    await openPage(page);

    const fieldset = page.getByRole("group", { name: T.keep.question });
    const boxes = fieldset.getByRole("checkbox");
    await expect(boxes).toHaveCount(3);
    const kept = fieldset.locator('[role="checkbox"][aria-checked="true"]');
    await expect(kept).toHaveCount(1);
    const live = page.getByText(fill(T.keep.count, { count: 1, max: 1 }), {
      exact: true,
    });
    await expect(live).toBeVisible();

    // Uncheck the kept workspace with Space; the others become reachable.
    const states = await boxes.evaluateAll((els) =>
      els.map((el) => el.getAttribute("aria-checked")),
    );
    const keptIsLast = states.indexOf("true") === states.length - 1;
    await kept.focus();
    await page.keyboard.press("Space");
    await expect(kept).toHaveCount(0);
    await expect(
      page.getByText(fill(T.keep.count, { count: 0, max: 1 }), { exact: true }),
    ).toBeVisible();

    // Tab to a neighbouring workspace and check it with Space.
    await page.keyboard.press(keptIsLast ? "Shift+Tab" : "Tab");
    const focused = page.locator(":focus");
    await expect(focused).toHaveAttribute("role", "checkbox");
    await expect(focused).toHaveAttribute("aria-checked", "false");
    await page.keyboard.press("Space");
    await expect(focused).toHaveAttribute("aria-checked", "true");
    await expect(live).toBeVisible();

    await page.getByRole("button", { name: T.keep.preview }).first().focus();
    await page.keyboard.press("Enter");
    const result = page.getByRole("region", { name: T.keep.previewTitle });
    await expect(result).toBeVisible({ timeout: 15_000 });
    const lists = result.getByRole("list");
    await expect(lists.nth(0).getByRole("listitem")).toHaveCount(1);
    await expect(lists.nth(1).getByRole("listitem")).toHaveCount(2);
    expect(saves).toEqual([]);
  });

  test("after two bonus slots all three are active again", async ({
    page,
    playwright,
  }) => {
    const admin = await playwright.request.newContext({
      storageState: ADMIN_STATE,
    });
    try {
      const userId = encodeURIComponent(`local:${SLOT_OWNER_LOGIN_ID}`);
      const response = await admin.patch(
        `${API_URL}/api/v1/admin/users/${userId}/workspace_slot_bonus`,
        { data: { delta: 2 } },
      );
      expect(
        response.ok(),
        `slot bonus failed (${response.status()}): ${await response.text()}`,
      ).toBe(true);
    } finally {
      await admin.dispose();
    }

    await openPage(page);
    await expect(
      page.getByText(fill(T.summary.active, { active: 3, slots: 3 }), {
        exact: true,
      }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(
      page
        .locator(ROWS)
        .filter({ has: page.getByText(T.status.active, { exact: true }) }),
    ).toHaveCount(3);
    await expect(page.getByRole("button", { name: T.actions.resume })).toHaveCount(0);
  });
});
