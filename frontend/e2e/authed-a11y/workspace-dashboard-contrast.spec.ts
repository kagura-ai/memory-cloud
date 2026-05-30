import { test } from "../fixtures/admin-auth";
import {
  assertNoColorContrastViolations,
  gotoAndWaitStable,
} from "../fixtures";

/**
 * Color-contrast a11y guard for /workspace/dashboard (Issue #785).
 *
 * NON-HERMETIC: requires a live backend + an authenticated admin (the
 * `adminAuth` auto-fixture logs in via the API). This is why the spec lives
 * under `e2e/authed-a11y/` — the path is deliberately NOT matched by the
 * hermetic `playwright test e2e/a11y` job (#786). Run locally via
 * `npm run test:a11y:authed` with E2E_ADMIN_* set and the stack up.
 *
 * Wiring these authenticated specs into a full-stack CI lane (backend services
 * + seed + storageState globalSetup) is the deferred follow-up issue.
 *
 * `trace: off` per the admin-auth fixture contract — the login POST body would
 * otherwise be captured in a retained trace on failure.
 */
test.use({ trace: "off" });

test.describe("/workspace/dashboard color-contrast (#785)", () => {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`${colorScheme} mode has no color-contrast violations`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await gotoAndWaitStable(page, "/workspace/dashboard");
      await assertNoColorContrastViolations(page);
    });
  }
});
