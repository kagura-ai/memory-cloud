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
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
});
