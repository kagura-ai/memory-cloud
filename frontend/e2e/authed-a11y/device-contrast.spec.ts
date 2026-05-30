import { test } from "../fixtures/admin-auth";
import {
  assertNoColorContrastViolations,
  gotoAndWaitStable,
} from "../fixtures";

/**
 * Color-contrast a11y guard for /device (Issue #785).
 *
 * /device is auth-guarded: unauthenticated users are redirected to /login
 * (see src/app/device/page.tsx). The `adminAuth` auto-fixture keeps us logged
 * in so the device-consent chrome (RFC 8628, #633 parity with /login) renders.
 * With no `user_code` query param the manual code-entry surface is shown —
 * sufficient for color-contrast coverage of the page's color tokens.
 *
 * NON-HERMETIC (needs a live backend + authenticated admin) → lives outside the
 * hermetic `e2e/a11y` job (#786). Run via `npm run test:a11y:authed`.
 */
test.use({ trace: "off" });

test.describe("/device color-contrast (#785)", () => {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`${colorScheme} mode has no color-contrast violations`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await gotoAndWaitStable(page, "/device");
      await assertNoColorContrastViolations(page);
    });
  }
});
