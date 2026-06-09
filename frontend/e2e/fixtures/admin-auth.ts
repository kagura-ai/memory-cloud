import { test, expect } from "@playwright/test";

/**
 * Admin auth helpers for authenticated E2E specs (#688, made deterministic in #959).
 *
 * Authentication is no longer performed per-test. Instead the `setup` project
 * (`e2e/auth.setup.ts`) logs the test admin in **once** before the suite and
 * persists the session cookie to a `storageState` file; the `authed` project in
 * `playwright.config.ts` injects that cookie into every test's browser context
 * via `use.storageState`. Specs under that project are therefore already
 * authenticated when they start — `page.goto("/admin/...")` just works.
 *
 * Why this matters (the #959 fix): the repo enforces single-session-per-user
 * (Issue #114 — `delete_user_sessions` runs on every login). The old fixture
 * re-logged-in on *every* test, so two parallel workers sharing the one
 * `e2e-admin` account would clobber each other's session → intermittent 401s.
 * Logging in exactly once removes the clobbering entirely. It also keeps the
 * password out of any traced browser context (only the `setup` project ever
 * sends it, and that project runs with `trace: "off"`).
 *
 * Specs import `test`/`expect` from here purely for a stable, documented import
 * path; the `test` is the unmodified Playwright base test. Auth comes from the
 * project's `storageState`, not from a fixture.
 *
 * Required env vars (read by `auth.setup.ts`; set before `npm run test:e2e`,
 * `npm run test:a11y:authed`, or `make test-e2e-frontend`):
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
 *   - Exist (create with `make admin`, or `python -m src.cli.seed_e2e_admin`)
 *   - Have role = admin
 *   - NOT have MFA enabled (TOTP fixture would require a shared secret)
 *
 * Cookie origin: the session cookie is scoped to the API origin
 * (`E2E_API_URL`). Subsequent cross-origin fetches from a `:3000` page work
 * because (a) `localhost:3000` and `localhost:8080` are the same SameSite
 * "site" (same eTLD+1 `localhost`), and (b) apiClient sets
 * `credentials: "include"` and backend CORS allows the Next.js origin.
 */

export const ADMIN_LOGIN_ID = process.env.E2E_ADMIN_LOGIN_ID ?? "";
export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "";
export const API_URL =
  process.env.E2E_API_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8080";

export { test, expect };
