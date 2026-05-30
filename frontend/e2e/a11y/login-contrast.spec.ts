import { test } from "@playwright/test";
import {
  assertNoColorContrastViolations,
  gotoAndWaitStable,
} from "../fixtures";

/**
 * Color-contrast a11y guard for /login (Issue #780 scaffold).
 *
 * Covers WCAG 2.1 AA color-contrast (1.4.3) in both light and dark mode via
 * prefers-color-scheme emulation. /login is a deliberate scaffold target
 * because it is always reachable without auth and exercises both the password
 * form and OAuth button surfaces. Hermetic — runs in the `frontend-a11y` CI
 * job (#786). The contrast/wait helpers are shared via `e2e/fixtures.ts` (#785).
 *
 * Authenticated-page coverage (/device, /invite/[token], /workspace/dashboard)
 * lives under `e2e/authed-a11y/` and is NOT part of the hermetic job.
 */
test.describe("/login color-contrast (#780 scaffold)", () => {
  test("light mode has no color-contrast violations", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await gotoAndWaitStable(page, "/login");
    await assertNoColorContrastViolations(page);
  });

  test("dark mode has no color-contrast violations", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await gotoAndWaitStable(page, "/login");
    await assertNoColorContrastViolations(page);
  });
});
