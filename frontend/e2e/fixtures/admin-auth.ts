import { test as base, expect } from "@playwright/test";

/**
 * Admin auth fixture for non-a11y E2E specs (#688).
 *
 * Logs the test admin in via the API (POST /api/v1/auth/login) and injects
 * the resulting session cookie into the browser context, so any subsequent
 * page.goto("/admin/...") is already authenticated.
 *
 * Why API login (not /login UI):
 * - Deterministic (no /login form rendering / OAuth button noise to wait on).
 * - Decouples admin-feature specs from /login UI changes — /login is a
 *   separate surface and exercised by frontend/e2e/a11y/login-contrast.spec.ts.
 *
 * Required env vars (set before `make test-e2e-frontend` or `npm run test:e2e`):
 *   E2E_ADMIN_LOGIN_ID   — login_id for an existing admin user without MFA
 *   E2E_ADMIN_PASSWORD   — that user's password
 * Optional:
 *   E2E_API_URL          — backend API base URL (default: NEXT_PUBLIC_API_URL
 *                          else http://localhost:8080). Required because the
 *                          Playwright config's baseURL points at Next.js
 *                          (:3000) for `page.goto`, but the API lives on a
 *                          different origin and there is no Next.js rewrite
 *                          for `/api/*`. Use `API_URL` (exported below) for
 *                          all direct API calls; reserve baseURL for
 *                          `page.goto`.
 *
 * The test admin must:
 *   - Exist (create with `make admin` if needed)
 *   - Have role = admin
 *   - NOT have MFA enabled (TOTP fixture would require a shared secret)
 *
 * Cookie capture: context.request.post() shares the cookie jar with browser
 * pages opened from the same context, so we don't need an explicit
 * `context.addCookies(...)` step after the login call. The session cookie
 * is scoped to the API origin (`E2E_API_URL`); subsequent cross-origin
 * fetches from a `:3000` page work because (a) `localhost:3000` and
 * `localhost:8080` are the same SameSite "site" (same eTLD+1 `localhost`),
 * and (b) apiClient sets `credentials: "include"` and backend CORS allows
 * the Next.js origin.
 */

const ADMIN_LOGIN_ID = process.env.E2E_ADMIN_LOGIN_ID ?? "";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "";
export const API_URL =
  process.env.E2E_API_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8080";

type AdminAuthFixtures = {
  adminAuth: void;
};

export const test = base.extend<AdminAuthFixtures>({
  adminAuth: [
    async ({ context }, use) => {
      if (!ADMIN_LOGIN_ID || !ADMIN_PASSWORD) {
        throw new Error(
          "E2E_ADMIN_LOGIN_ID and E2E_ADMIN_PASSWORD must be set. " +
            "See frontend/e2e/README.md for setup.",
        );
      }

      const response = await context.request.post(
        `${API_URL}/api/v1/auth/login`,
        {
          data: { login_id: ADMIN_LOGIN_ID, password: ADMIN_PASSWORD },
        },
      );
      expect(
        response.ok(),
        `admin login failed (${response.status()}): ${await response.text()}`,
      ).toBe(true);

      const body = await response.json();
      if (body.mfa_required) {
        throw new Error(
          "E2E admin has MFA enabled — disable MFA on the test admin " +
            "or use a dedicated non-MFA admin for E2E.",
        );
      }

      await use();
    },
    { auto: true, scope: "test" },
  ],
});

export { expect };
