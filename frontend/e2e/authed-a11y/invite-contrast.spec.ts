import { test } from "@playwright/test";
import {
  assertNoColorContrastViolations,
  gotoAndWaitStable,
} from "../fixtures";

/**
 * Color-contrast a11y guard for /invite/[token] (Issue #785).
 *
 * /invite is a public route, but its content is backend-driven: the page fetches
 * GET /api/v1/invitations/{token} and branches on the result
 * (src/app/invite/[token]/page.tsx). This spec covers the reachable-WITHOUT-seed
 * state — an unknown token resolves to the error screen — so it needs a live
 * backend but no Pro-plan workspace or seeded invitation.
 *
 * Contrast-testing the fully-seeded happy paths ("login_required" / "email
 * mismatch" / "success" screens, which require a Pro-plan workspace + a created
 * invitation) is part of the deferred full-stack a11y CI lane (see the #785
 * follow-up issue). NON-HERMETIC → outside the hermetic `e2e/a11y` job (#786).
 */
test.describe("/invite/[token] color-contrast (#785)", () => {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`${colorScheme} mode (unknown token → error screen) has no color-contrast violations`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await gotoAndWaitStable(page, "/invite/e2e-a11y-nonexistent-token");
      await assertNoColorContrastViolations(page);
    });
  }
});
