import { test } from "@playwright/test";
import {
  assertNoColorContrastViolations,
  gotoAndWaitStable,
} from "../fixtures";

/**
 * Color-contrast a11y guard for the destructive Alert error state (#957).
 *
 * /signup-blocked is hermetic (no auth) and unconditionally renders an
 * `<Alert variant="destructive">`, so it deterministically exercises the
 * destructive Alert palette that previously failed WCAG AA — ~3.76:1 in light
 * and 1.78:1 in dark (red-on-dark). That bug only ever surfaced intermittently
 * via the authed-a11y dashboard flake; this spec catches regressions in the
 * hermetic `frontend-a11y` job without needing auth.
 */
test.describe("/signup-blocked color-contrast (#957)", () => {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`${colorScheme} mode has no color-contrast violations`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await gotoAndWaitStable(page, "/signup-blocked", "[role='alert']");
      await assertNoColorContrastViolations(page);
    });
  }
});
