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
const LANDMARK_TIMEOUT_MS = 15_000;
// How many times a scan may be repeated after the document reloaded under it.
const RELOAD_RESCANS = 2;

// The selector each page was declared settled on (by `gotoAndWaitStable`), so
// a rescan waits for the same screen the spec waited for, not the broad default.
const settledLandmark = new WeakMap<Page, string>();

/** Wait until the element that marks the screen as settled is visible. */
async function waitForLandmark(page: Page, landmark: string): Promise<void> {
  await page.locator(landmark).first().waitFor({
    state: "visible",
    timeout: LANDMARK_TIMEOUT_MS,
  });
}

/**
 * Assert WCAG 2.1 AA color-contrast (1.4.3) has no violations on the current page.
 *
 * `landmark` marks the screen as settled. It defaults to the selector the spec
 * gave `gotoAndWaitStable` for this page, and a spec that waited for something
 * narrower afterwards passes that instead.
 *
 * `next dev` can reload the document under a running scan — the route finishes
 * compiling on its first request and Fast Refresh falls back to a full reload —
 * which surfaces as `page.evaluate: Execution context was destroyed` (or one of
 * its siblings) inside `analyze()` (#1824). That is a navigation, not a
 * finding: when the page is still on the same URL, wait for the landmark on
 * the new document and rescan, a bounded number of times. Every other error,
 * including a genuine contrast violation, is rethrown as it is.
 */
export async function assertNoColorContrastViolations(
  page: Page,
  landmark = settledLandmark.get(page) ?? DEFAULT_LANDMARK,
): Promise<void> {
  // A reload lands on the same URL; a redirect does not, and must not pass
  // as "the same screen, scanned again".
  const url = page.url();
  for (let rescans = 0; ; rescans++) {
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
      if (!isNavigationError(error)) throw error;
      if (rescans >= RELOAD_RESCANS) {
        throw new Error(
          `the page reloaded under the scan ${rescans + 1} times (#1824)`,
          { cause: error },
        );
      }
      await page.waitForLoadState("domcontentloaded");
      expect(page.url(), "the page navigated away during the scan").toBe(url);
      await waitForLandmark(page, landmark);
    }
  }
}

/**
 * A navigation tore down the page axe was scanning. Playwright reports it with
 * one of several messages depending on which evaluate the reload interrupted.
 */
function isNavigationError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /Execution context was destroyed|because of a navigation|Cannot find context with specified id|Frame was detached/i.test(
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
 *
 * The landmark is remembered for `assertNoColorContrastViolations`, so a rescan
 * after a dev-server reload waits for the same screen.
 */
export async function gotoAndWaitStable(
  page: Page,
  path: string,
  landmark = DEFAULT_LANDMARK,
): Promise<void> {
  settledLandmark.set(page, landmark);
  const response = await page.goto(path, { waitUntil: "domcontentloaded" });
  if (response && response.status() >= 500) {
    await page.waitForTimeout(1_000);
    await page.goto(path, { waitUntil: "domcontentloaded" });
  }
  await waitForLandmark(page, landmark);
}
