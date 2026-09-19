import { test } from "@playwright/test";
import {
  assertNoColorContrastViolations,
  gotoAndWaitStable,
} from "../fixtures";

/**
 * Color-contrast a11y guard for the beta invite landing page (#1582).
 *
 * /join/[token] is a public route whose content is backend-driven
 * (src/app/join/[token]/page.tsx). In this hermetic lane there is no backend:
 * the session check and the preview both fail at the network, which is neither
 * a 404 nor a 410, so the page deterministically settles on the retryable
 * `error` screen without waiting for the feature flags. That is the same card
 * shell, heading and body palette every other state renders with, plus the
 * primary Retry button.
 *
 * The backend-driven states (`valid` with its provider buttons, `invalid`,
 * `expired`, `disabled`, `already_signed_in`) need a live backend, so they
 * belong to the authed / full-stack lane, not here (#786).
 *
 * Wait on the <h1>, NOT the default landmark set: the loading screen already
 * renders <main>, so the default would resolve while the spinner is still up
 * and axe would check the wrong screen.
 */
test.describe("/join/[token] color-contrast (#1582)", () => {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`${colorScheme} mode (no backend → error screen) has no color-contrast violations`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await gotoAndWaitStable(page, "/join/e2e-a11y-nonexistent-token", "h1");
      await assertNoColorContrastViolations(page);
    });
  }
});
