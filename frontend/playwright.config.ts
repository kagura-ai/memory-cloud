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
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://localhost:3000",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
});
