import { test as setup } from "@playwright/test";

/**
 * Route warm-up for the `authed` Playwright project (#1500).
 *
 * The CI lanes run `next dev`, which compiles a route on its first request.
 * When two specs hit two cold routes at once, the dev server can answer the
 * first request with a 500 ("SyntaxError: Unexpected end of JSON input", then
 * "Fast Refresh had to perform a full reload") that a second request no longer
 * reproduces — so a spec waiting on the page's landmark times out although
 * nothing in the app is wrong. Both runs behind #1500 failed exactly this way
 * while the API had been healthy for 26s.
 *
 * Compiling every route the authed specs visit ONCE, serially, before any spec
 * runs removes the race at its source and keeps dev-mode HMR for the specs.
 * Routes behind auth redirect to /login for this cookie-less request; that
 * still compiles them. A route that is missing here still has the one-retry
 * backstop in `gotoAndWaitStable` (e2e/fixtures.ts).
 */

/** Every route the `authed` project navigates to, plus its login redirect target. */
const WARM_ROUTES = [
  "/login",
  "/device",
  "/invite/e2e-a11y-warmup-token",
  "/workspace/dashboard",
  "/profile",
  "/admin/users/e2e-a11y-warmup-user",
];

const ATTEMPTS = 5;
const RETRY_DELAY_MS = 2_000;

setup("warm up authed routes", async ({ request }) => {
  setup.setTimeout(ATTEMPTS * RETRY_DELAY_MS * WARM_ROUTES.length + 60_000);
  for (const route of WARM_ROUTES) {
    let lastStatus = 0;
    for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
      const response = await request.get(route, { failOnStatusCode: false });
      lastStatus = response.status();
      if (lastStatus < 500) break;
      await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS));
    }
    if (lastStatus >= 500) {
      throw new Error(
        `warm-up: ${route} still answered ${lastStatus} after ${ATTEMPTS} attempts`,
      );
    }
  }
});
