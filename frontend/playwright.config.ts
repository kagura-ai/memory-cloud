import { defineConfig } from "@playwright/test";

/**
 * Playwright config for a11y / e2e tests (Issue #780).
 *
 * Currently scoped to color-contrast checks via @axe-core/playwright. Local
 * runs auto-start `next dev` on :3000. CI integration is a follow-up.
 *
 * To add coverage for more pages, drop new specs into `e2e/a11y/`.
 */
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
    {
      // Mock OAuth IdP for the account-linking E2E (#937). Idle for other specs;
      // the backend only calls it during the link round-trip. Kept in the shared
      // webServer list (not a per-project hook) so `npm run test:e2e:oauth` is
      // self-contained locally; in CI it is likewise managed by Playwright.
      command: "npm run mock-idp",
      url: `http://localhost:${process.env.MOCK_IDP_PORT ?? 9100}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
});
