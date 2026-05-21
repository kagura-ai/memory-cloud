import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/**
 * Color-contrast a11y guard for /login (Issue #780 scaffold).
 *
 * Covers WCAG 2.1 AA color-contrast (1.4.3) in both light and dark mode via
 * prefers-color-scheme emulation. /login is a deliberate scaffold target
 * because it is always reachable without auth and exercises both the password
 * form and OAuth button surfaces.
 *
 * Broader coverage (/device, /invite/[token], /workspace/dashboard) is tracked
 * as a follow-up.
 */
async function assertNoColorContrastViolations(
  page: import("@playwright/test").Page,
) {
  const results = await new AxeBuilder({ page })
    .options({ runOnly: ["color-contrast"] })
    .analyze();

  expect(
    results.violations,
    JSON.stringify(results.violations, null, 2),
  ).toEqual([]);
}

test.describe("/login color-contrast (#780 scaffold)", () => {
  test("light mode has no color-contrast violations", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/login");
    await page.waitForLoadState("networkidle");
    await assertNoColorContrastViolations(page);
  });

  test("dark mode has no color-contrast violations", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await page.goto("/login");
    await page.waitForLoadState("networkidle");
    await assertNoColorContrastViolations(page);
  });
});
