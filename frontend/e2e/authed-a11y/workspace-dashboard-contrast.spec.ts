import { expect, test } from "../fixtures/admin-auth";
import {
  assertNoColorContrastViolations,
  gotoAndWaitStable,
} from "../fixtures";

/**
 * Color-contrast a11y guard for /workspace/dashboard (Issue #785).
 *
 * NON-HERMETIC: requires a live backend + an authenticated admin. Auth comes
 * from the `authed` project's pre-seeded `storageState` (#959). This is why the
 * spec lives under `e2e/authed-a11y/` — the path is deliberately NOT matched by
 * the hermetic `playwright test e2e/a11y` job (#786). Run locally via
 * `npm run test:a11y:authed` with E2E_ADMIN_* set and the stack up.
 *
 * Trace capture stays on (project default): the password never reaches this
 * spec — only the `setup` project's login POST handles it, with trace off (#959).
 */
test.describe("/workspace/dashboard color-contrast (#785)", () => {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`${colorScheme} mode has no color-contrast violations`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await gotoAndWaitStable(page, "/workspace/dashboard");
      // Assert we rendered the REAL authenticated dashboard, not the /login
      // redirect (unauthenticated) or the "Not authenticated" error state that
      // a clobbered session (#959) produced. The seeded admin owns a Pro
      // workspace, so it is not redirected to /workspace/contexts (viewer-only).
      await expect(page).toHaveURL(/\/workspace\/dashboard(\?|$)/);
      await expect(page.locator("h1")).toBeVisible();
      // The <h1> is up while the stats still load, and the loading text was
      // what axe kept catching in dark mode (#1824). Wait for the KPI cards:
      // a positive signal that the stats rendered, not the absence of the
      // spinner (which would also pass before it ever appeared).
      await expect(page.getByTestId("kpi-cards")).toBeVisible({
        timeout: 15_000,
      });
      await assertNoColorContrastViolations(page, '[data-testid="kpi-cards"]');
    });
  }
});
