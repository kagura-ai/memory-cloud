import { test, expect, API_URL } from "./fixtures/admin-auth";
import { CONNECTED_ACCOUNTS_TEST_IDS as T } from "@/components/auth/connected-accounts.testids";

// Trace capture stays on (project default `retain-on-failure`). Auth comes from
// the `authed` project's pre-seeded `storageState`; the password is only sent
// by the `setup` project's login POST (trace off), so no credential reaches a
// trace from this spec (#959, supersedes the PR #807 per-spec opt-out).

/**
 * Account-linking E2E: link → unlink → re-link (Issue #937, follow-up to #517).
 *
 * Closes the E2E gap #517 deferred. The backend performs the OAuth token +
 * userinfo exchange server-side, so a browser mock cannot intercept it; instead
 * a real mock IdP (frontend/e2e/mock-idp/server.mjs) is stood up and the backend
 * is pointed at it via OAUTH_*_URL overrides (gated by
 * OAUTH_ENDPOINT_OVERRIDE_ENABLED, hard-blocked in production — see
 * backend/src/auth/oauth_endpoints.py + tests/auth/test_oauth_endpoints.py).
 *
 * Coverage map for #517 account-linking:
 * - unit (Vitest)        → ConnectedAccounts.test.tsx (incl. last-method-blocked UI)
 * - integration (pytest) → test_account_linking.py / test_link_callback.py
 * - E2E (this spec)      → real browser → backend → mock-IdP → DB round-trip
 *
 * The "last-method Disconnect is blocked" case is NOT re-covered here: the admin
 * fixture user is a password user (isOnlyMethod is always false), so that state
 * is unreachable without seeding a dedicated OAuth-only user. It is fully covered
 * by ConnectedAccounts.test.tsx ("disables Disconnect … only sign-in method").
 *
 * Locators are testid-only (locale-independent) per #688 gate1 (QA Lead). Linked
 * vs unlinked state is asserted via which button is present (connect ⇄ disconnect),
 * not via the locale-dependent "Connected"/"Not connected" status text.
 */

const PROVIDER = "github";
const connectBtn = T.connect(PROVIDER);
const disconnectBtn = T.disconnect(PROVIDER);

test.beforeEach(async ({ context }) => {
  // Idempotent pre-clean so re-runs against a non-ephemeral DB start unlinked.
  // 404 (not linked) / 409 (would-be last method — impossible for the password
  // admin) are both fine to ignore.
  await context.request.post(`${API_URL}/api/v1/me/account/unlink-provider`, {
    data: { provider: PROVIDER },
  });
});

test("link → unlink → re-link a GitHub identity via the mock IdP", async ({
  page,
}) => {
  await page.goto("/profile");

  // Starts unlinked: the Connect affordance is present, Disconnect is not.
  await expect(page.getByTestId(connectBtn)).toBeVisible();
  await expect(page.getByTestId(disconnectBtn)).toHaveCount(0);

  // --- Link: full-page OAuth round-trip (frontend → mock IdP → backend → /profile). ---
  // Assert on DOM state (the Disconnect button appears only after the round-trip
  // lands back on /profile AND the providers fetch reports github linked), not on
  // the URL: waitForURL(/profile/) would match the *current* /profile before the
  // navigation even starts, and the success "?linked=1" param persists across the
  // later unlink — both would resolve early. The 30s timeout covers the full
  // browser → mock IdP → backend → DB → redirect → reload chain on a cold runner.
  await page.getByTestId(connectBtn).click();
  await expect(page.getByTestId(disconnectBtn)).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByTestId(connectBtn)).toHaveCount(0);

  // --- Unlink: open the confirm dialog and confirm (in-page, no navigation). ---
  await page.getByTestId(disconnectBtn).click();
  await page.getByTestId(T.disconnectConfirm).click();

  // Back to unlinked (the component reloads providers on success).
  await expect(page.getByTestId(connectBtn)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId(disconnectBtn)).toHaveCount(0);

  // --- Re-link: the same identity links again cleanly. ---
  await page.getByTestId(connectBtn).click();
  await expect(page.getByTestId(disconnectBtn)).toBeVisible({
    timeout: 30_000,
  });
});
