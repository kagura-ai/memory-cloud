import { defineConfig } from "@playwright/test";
import path from "node:path";

/**
 * Playwright config for a11y / e2e tests (Issue #780).
 *
 * Three projects (#959):
 *   - `setup`    — logs the test admin in once (e2e/auth.setup.ts) and writes a
 *                  storageState file. Runs with trace off so the login POST
 *                  body (the admin password) is never captured.
 *   - `hermetic` — no-auth, no-backend color-contrast specs (e2e/a11y/). Has no
 *                  dependency on `setup`, so the hermetic CI lane stays
 *                  backend-free. Run via `npm run test:a11y`.
 *   - `authed`   — backend-dependent specs (e2e/authed-a11y/, admin-*, OAuth).
 *                  Depends on `setup` and reuses its session cookie via
 *                  `use.storageState`, so there is no per-test re-login. This is
 *                  what makes `frontend-a11y-authed` deterministic: with
 *                  single-session-per-user (#114) a per-test login let parallel
 *                  workers clobber each other's session. Run authed-a11y via
 *                  `npm run test:a11y:authed`.
 *
 * To add coverage for more pages, drop new specs into `e2e/a11y/` (hermetic) or
 * `e2e/authed-a11y/` (authenticated).
 */

// Session cookie produced by the `setup` project and consumed by `authed`.
// Must match `STORAGE_STATE` in e2e/auth.setup.ts. Gitignored (e2e/.auth/).
const STORAGE_STATE = path.join(__dirname, "e2e", ".auth", "admin.json");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  // CI: `github` emits inline log annotations; `html` writes the
  // `playwright-report/` directory that the frontend-a11y job uploads as a
  // failure artifact (#786). `open: never` keeps the run non-interactive.
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://localhost:3000",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      // In CI, force the webpack compiler — Turbopack (the Next.js 16 default)
      // can't load its native @next/swc binding on the GitHub Actions runner,
      // the same failure that breaks `next build` there. Locally, keep the
      // default (Turbopack) for the faster dev experience. See #855.
      command: process.env.CI ? "npm run dev -- --webpack" : "npm run dev",
      url: "http://localhost:3000",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    // Mock OAuth IdP for the account-linking E2E (#937). Gated on PW_OAUTH_IDP
    // (set by `npm run test:e2e:oauth`) so the a11y lanes are NOT coupled to the
    // mock's health — a bug in the mock must never fail an unrelated a11y run.
    ...(process.env.PW_OAUTH_IDP === "1"
      ? [
          {
            command: "npm run mock-idp",
            url: `http://localhost:${process.env.MOCK_IDP_PORT ?? 9100}/health`,
            reuseExistingServer: !process.env.CI,
            timeout: 30_000,
            stdout: "pipe" as const,
            stderr: "pipe" as const,
          },
        ]
      : []),
  ],
  projects: [
    {
      name: "setup",
      testMatch: /auth\.setup\.ts$/,
      // The login POST carries E2E_ADMIN_PASSWORD — never retain it in a trace.
      use: { trace: "off" },
    },
    {
      name: "hermetic",
      testMatch: /e2e\/a11y\/.*\.spec\.ts$/,
      use: { browserName: "chromium" },
    },
    {
      name: "authed",
      testMatch:
        /e2e\/(authed-a11y\/.*|admin-.*|oauth-account-linking)\.spec\.ts$/,
      dependencies: ["setup"],
      use: { browserName: "chromium", storageState: STORAGE_STATE },
    },
  ],
});
