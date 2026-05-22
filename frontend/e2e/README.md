# Frontend a11y / e2e tests (Playwright + axe-core)

Issue #780 introduced this directory with a single color-contrast spec for `/login`.
Run locally:

```bash
cd frontend
npx playwright install chromium  # first time only
npm run test:e2e
```

`npm run test:e2e` auto-starts `next dev` on :3000 via the `webServer` block in
`playwright.config.ts`. If you already have a dev server running, the existing
process is reused (`reuseExistingServer: !process.env.CI`).

## Adding a new page

Drop a new spec into `e2e/a11y/<page>-contrast.spec.ts` following the
`login-contrast.spec.ts` shape — `await page.goto("/<path>")` then
`await new AxeBuilder({ page }).options({ runOnly: ["color-contrast"] }).analyze()`.

## CI

Not yet wired into `.github/workflows/ci.yml` — tracked as a follow-up.
