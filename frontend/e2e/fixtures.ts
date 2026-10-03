import { expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/**
 * Shared a11y test helpers (#780 scaffold, extracted in #785).
 *
 * Keep this module auth-free so hermetic specs under `e2e/a11y/` can import it
 * without pulling in the admin-auth fixture. Authenticated specs live under
 * `e2e/authed-a11y/` and combine these helpers with `e2e/fixtures/admin-auth`.
 */

const DEFAULT_LANDMARK = "h1, form, main button, main";

/**
 * Assert WCAG 2.1 AA color-contrast (1.4.3) has no violations on the current page.
 *
 * `landmark` is the element that marks the screen as settled (the same
 * selector the spec gave `gotoAndWaitStable`). `next dev` can reload the
 * document under a running scan — the route finishes compiling on its first
 * request and Fast Refresh falls back to a full reload — which surfaces as
 * `page.evaluate: Execution context was destroyed` inside `analyze()`
 * (#1824). That is a navigation, not a finding: wait for the landmark again
 * on the new document and rescan, a bounded number of times.
 */
export async function assertNoColorContrastViolations(
  page: Page,
  landmark = DEFAULT_LANDMARK,
): Promise<void> {
  const attempts = 3;
  // A reload lands on the same URL; a redirect does not, and must not pass
  // as "the same screen, scanned again".
  const url = page.url();
  for (let attempt = 1; ; attempt++) {
    try {
      const results = await new AxeBuilder({ page })
        .options({ runOnly: ["color-contrast"] })
        .analyze();
      expect(
        results.violations,
        JSON.stringify(results.violations, null, 2),
      ).toEqual([]);
      return;
    } catch (error) {
      if (attempt >= attempts || !isNavigationError(error)) throw error;
      await page.waitForLoadState("domcontentloaded");
      expect(page.url(), "the page navigated away during the scan").toBe(url);
      await page.locator(landmark).first().waitFor({
        state: "visible",
        timeout: 15_000,
      });
    }
  }
}

/** A navigation tore down the page axe was scanning. */
function isNavigationError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /Execution context was destroyed|because of a navigation/i.test(
    message,
  );
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
  landmark = DEFAULT_LANDMARK,
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
