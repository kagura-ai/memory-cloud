import { expect, test } from "../fixtures/admin-auth";
import {
  assertNoColorContrastViolations,
  gotoAndWaitStable,
} from "../fixtures";

/**
 * Color-contrast a11y guard for /device (Issue #785).
 *
 * /device is auth-guarded: unauthenticated users are redirected to /login
 * (see src/app/device/page.tsx). Auth comes from the `authed` project's
 * pre-seeded `storageState` (#959), so the device-consent chrome (RFC 8628,
 * #633 parity with /login) renders. With no `user_code` query param the manual
 * code-entry surface is shown — sufficient for color-contrast coverage.
 *
 * NON-HERMETIC (needs a live backend + authenticated admin) → lives outside the
 * hermetic `e2e/a11y` job (#786). Run via `npm run test:a11y:authed`.
 *
 * Trace capture stays on (project default): the password never reaches this
 * spec — only the `setup` project's login POST handles it, with trace off (#959).
 */
test.describe("/device color-contrast (#785)", () => {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`${colorScheme} mode has no color-contrast violations`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await gotoAndWaitStable(page, "/device");
      // Assert we landed on the REAL authenticated device surface, not the
      // /login redirect that an unauthenticated (or clobbered-session, #959)
      // request would produce. The code-entry input only renders once the auth
      // guard passes; #userCode is locale-independent.
      await expect(page).toHaveURL(/\/device(\?|$)/);
      await expect(page.locator("#userCode")).toBeVisible();
      await assertNoColorContrastViolations(page);
    });
  }
});
