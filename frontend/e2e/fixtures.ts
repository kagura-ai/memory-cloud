import { expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/**
 * Shared a11y test helpers (#780 scaffold, extracted in #785).
 *
 * Keep this module auth-free so hermetic specs under `e2e/a11y/` can import it
 * without pulling in the admin-auth fixture. Authenticated specs live under
 * `e2e/authed-a11y/` and combine these helpers with `e2e/fixtures/admin-auth`.
 */

/** Assert WCAG 2.1 AA color-contrast (1.4.3) has no violations on the current page. */
export async function assertNoColorContrastViolations(
  page: Page,
): Promise<void> {
  const results = await new AxeBuilder({ page })
    .options({ runOnly: ["color-contrast"] })
    .analyze();

  expect(
    results.violations,
    JSON.stringify(results.violations, null, 2),
  ).toEqual([]);
}

/**
 * Navigate to `path` and wait on a stable DOM signal.
 *
 * `networkidle` is unreliable against `next dev` because the HMR websocket keeps
 * the network busy indefinitely (Issue #780, surfaced by Copilot on PR #790).
 * Wait on a visible landmark instead. The default landmark set is broad enough
 * to cover form, hero, and authenticated-shell layouts.
 *
 * One retry on a 5xx: `next dev` compiles a route on its first request, and two
 * routes compiling at once can make the first response a 500 ("Unexpected end
 * of JSON input") that the next request no longer reproduces (#1500). The
 * `setup` project warms every route first (e2e/warmup.setup.ts); this is the
 * backstop for a route that was not on that list.
 */
export async function gotoAndWaitStable(
  page: Page,
  path: string,
  landmark = "h1, form, main button, main",
): Promise<void> {
  const response = await page.goto(path, { waitUntil: "domcontentloaded" });
  if (response && response.status() >= 500) {
    await page.waitForTimeout(1_000);
    await page.goto(path, { waitUntil: "domcontentloaded" });
  }
  await page.locator(landmark).first().waitFor({
    state: "visible",
    timeout: 15_000,
  });
}
