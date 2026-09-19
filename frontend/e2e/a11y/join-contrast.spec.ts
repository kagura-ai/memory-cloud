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
 * the session check and the preview both fail, and `useSystemFeatures` falls
 * back to "every flag off" once its retries are spent (~1.5s), so the page
 * deterministically settles on the `disabled` screen. That is the same card
 * shell, heading and body palette every other state renders with.
 *
 * The backend-driven states (`valid` with its provider buttons, `invalid`,
 * `expired`, `already_signed_in`) need a live backend with beta invites on, so
 * they belong to the authed / full-stack lane, not here (#786).
 *
 * Wait on the <h1>, NOT the default landmark set: the loading screen already
 * renders <main>, so the default would resolve while the spinner is still up
 * and axe would check the wrong screen.
 */
test.describe("/join/[token] color-contrast (#1582)", () => {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`${colorScheme} mode (no backend → disabled screen) has no color-contrast violations`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await gotoAndWaitStable(page, "/join/e2e-a11y-nonexistent-token", "h1");
      await assertNoColorContrastViolations(page);
    });
  }
});
