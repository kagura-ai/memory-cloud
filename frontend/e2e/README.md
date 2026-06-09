# Frontend a11y / e2e tests (Playwright + axe-core)

`/login` color-contrast spec was introduced by #780; `/admin/users/[userId]`
smoke spec by #688. Run locally:

```bash
cd frontend
npx playwright install chromium  # first time only
npm run test:e2e
# or from repo root:
make test-e2e-frontend
```

`npm run test:e2e` auto-starts `next dev` on :3000 via the `webServer` block
in `playwright.config.ts`. If you already have a dev server running, the
existing process is reused (`reuseExistingServer: !process.env.CI`).

## Adding a new a11y page

### Hermetic pages (no auth, no backend) → `e2e/a11y/`

Drop a new spec into `e2e/a11y/<page>-contrast.spec.ts` following the
`login-contrast.spec.ts` shape, using the shared helpers from `e2e/fixtures.ts`:

```ts
import { test } from "@playwright/test";
import { assertNoColorContrastViolations, gotoAndWaitStable } from "../fixtures";

test("light mode", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await gotoAndWaitStable(page, "/your-path");
  await assertNoColorContrastViolations(page);
});
```

Specs here are run by the hermetic `frontend-a11y` CI job (#786): only a
`next dev` server is started — **no backend**. Only put pages that render
meaningfully without auth or seeded data here.

### Authenticated / backend-dependent pages → `e2e/authed-a11y/` (#785)

Pages that need a live backend, an authenticated user, or seeded data
(`/device`, `/invite/[token]`, `/workspace/dashboard`) live under
`e2e/authed-a11y/` instead. The path is deliberately **not** matched by the
hermetic `playwright test e2e/a11y` glob, so these never run in the
backend-free CI job. Run them locally with the stack up:

```bash
export E2E_ADMIN_LOGIN_ID="admin"
export E2E_ADMIN_PASSWORD="<password>"
export E2E_API_URL="http://localhost:8080"
npm run test:a11y:authed
```

Authenticated specs combine `assertNoColorContrastViolations` / `gotoAndWaitStable`
from `e2e/fixtures.ts` with the `test` from `e2e/fixtures/admin-auth.ts`.

> **In CI** as the `frontend-a11y-authed` lane (`.github/workflows/ci.yml`),
> which stands up postgres + redis + a migrated schema + a seeded admin
> (`python -m src.cli.seed_e2e_admin`), starts the API, then runs
> `npm run test:a11y:authed`. Contrast-testing the fully-seeded happy paths
> (e.g. a valid invitation, which needs a Pro-plan workspace + a created
> invitation) is still a deferred follow-up to #785; the specs here currently
> cover the states reachable without that extra seed (e.g. `/invite` with an
> unknown token → error screen).

### How authentication works (deterministic, #959)

The `authed` Playwright project does **not** log in per test. Instead a `setup`
project (`e2e/auth.setup.ts`) runs first — as a declared dependency — and logs
the test admin in **exactly once** via `POST /api/v1/auth/login`, persisting the
session cookie to `e2e/.auth/admin.json` (gitignored). The `authed` project then
loads that cookie into every test's browser context via `use.storageState`.

This is load-bearing, not a convenience: the backend enforces
single-session-per-user (Issue #114 — `delete_user_sessions` runs on **every**
login). When the old fixture re-logged-in per test, two parallel Playwright
workers sharing the one `e2e-admin` account would clobber each other's session,
producing intermittent 401s ("Not authenticated"). Logging in once removes the
race. It also keeps the password out of every traced browser context — only the
`setup` project ever sends it, and that project runs with `trace: "off"`.

`npm run test:a11y` selects the backend-free `hermetic` project; it has **no**
dependency on `setup`, so the hermetic CI lane never attempts a login.

## OAuth account-linking E2E (mock IdP) — `oauth-account-linking.spec.ts` (#937)

Covers the link → unlink → re-link flow #517 deferred. The backend performs the
OAuth token + userinfo exchange **server-side**, so a browser-level mock cannot
intercept it. Instead a real mock IdP (`e2e/mock-idp/server.mjs`, zero-dependency
Node) is started by Playwright's `webServer`, and the backend is pointed at it
via `OAUTH_*_URL` overrides.

Those overrides are gated by `OAUTH_ENDPOINT_OVERRIDE_ENABLED` and **hard-blocked
in production** — `backend/src/auth/oauth_endpoints.py::assert_oauth_endpoints_safe`
(called at app boot) refuses to start if any override is set while
`ENVIRONMENT=production`. See `backend/tests/auth/test_oauth_endpoints.py`.

Run locally with the stack up (start the API with the overrides pointing at the
mock IdP on :9100; Playwright manages the mock IdP + `next dev` itself):

```bash
# backend (separate shell) — overrides + dummy GitHub creds
export ENVIRONMENT=development
export OAUTH_ENDPOINT_OVERRIDE_ENABLED=true
export OAUTH_GITHUB_AUTH_URL=http://localhost:9100/github/login/oauth/authorize
export OAUTH_GITHUB_TOKEN_URL=http://localhost:9100/github/login/oauth/access_token
export OAUTH_GITHUB_USER_URL=http://localhost:9100/github/user
export OAUTH_GITHUB_EMAILS_URL=http://localhost:9100/github/user/emails
export GITHUB_CLIENT_ID=e2e-mock-client-id
export GITHUB_CLIENT_SECRET=e2e-mock-client-secret
export GITHUB_REDIRECT_URI=http://localhost:8080/api/v1/auth/github/callback
# ...start uvicorn as usual...

# frontend
export E2E_ADMIN_LOGIN_ID=e2e-admin
export E2E_ADMIN_PASSWORD="<password>"
export E2E_API_URL=http://localhost:8080
npm run test:e2e:oauth
```

CI runs this as the `frontend-e2e-oauth` lane (`.github/workflows/ci.yml`),
modeled on `frontend-a11y-authed`. Like that lane it is a new CI-only check, not
yet Required (#768); boot/timing may need tuning over the first runs. The
"last-method Disconnect is blocked" case is covered by `ConnectedAccounts.test.tsx`
(the admin fixture user is a password user, so that UI state is unreachable in
this E2E without seeding a dedicated OAuth-only user).

## Adding a new authenticated admin spec

Use the admin auth fixture at `e2e/fixtures/admin-auth.ts`:

```ts
import { test, expect } from "./fixtures/admin-auth";

test("my admin smoke", async ({ page }) => {
  await page.goto("/admin/your-feature");
  await page.getByTestId("your-feature-anchor").waitFor({ state: "visible" });
  // ...
});
```

Auth is provided by the `authed` project's pre-seeded `storageState` (see "How
authentication works" above), so `page.goto("/admin/...")` is already
authenticated — no per-spec login and no `test.use({ trace: "off" })` needed
(the password never reaches the spec). New authed root specs must match the
`authed` project's `testMatch` in `playwright.config.ts` (`admin-*.spec.ts` or
add an entry); a spec that doesn't match runs with no `storageState` and 401s.

**Required env vars** before running an admin spec:

```bash
export E2E_ADMIN_LOGIN_ID="admin"          # login_id for an admin user
export E2E_ADMIN_PASSWORD="<password>"     # that user's password
# Optional — backend API base URL (default falls back to NEXT_PUBLIC_API_URL,
# then http://localhost:8080). Override if your backend listens elsewhere.
export E2E_API_URL="http://localhost:8080"
```

The test admin must (a) exist (create with `make admin`), (b) have role =
admin, (c) NOT have MFA enabled.

**API origin vs Next.js origin.** Playwright's `baseURL` points at Next.js
(`:3000`) so `page.goto("/admin/...")` works as expected. Direct API calls
(login, list users, etc.) MUST use the `API_URL` constant exported from
`admin-auth.ts` because there is no Next.js `/api/*` rewrite — a relative
`/api/...` path on `:3000` would 404. The session cookie set by the API on
`:8080` flows to subsequent browser fetches because `localhost:3000` and
`localhost:8080` are the same SameSite "site", and the frontend's apiClient
sets `credentials: "include"`.

**Locator discipline** (#688 / QA Lead gate1): use `getByTestId`, `getByRole`,
`getByLabel` — never `getByText`. Page content is localized through
`next-intl` and would shift under `lang != "en"` browser sessions.

## CI

A11y job (`/login` contrast) is tracked for `.github/workflows/ci.yml` wiring
in #786. Admin smoke specs (#688) require backend + DB up and admin credentials
seeded — CI wiring is deferred until secret management for E2E admin
credentials is decided.

### CI prerequisites for admin specs

Before wiring `make test-e2e-frontend` into CI, address these:

1. **Secret management for `E2E_ADMIN_LOGIN_ID` / `E2E_ADMIN_PASSWORD`.**
   GitHub Actions secrets, vault, or equivalent — never commit.
2. **Password leak in failure traces — RESOLVED (#959).** The login now happens
   only in the `setup` project (`e2e/auth.setup.ts`), which runs with
   `trace: "off"`; authed specs reuse the resulting `storageState` cookie and
   never send the password, so they keep the default `retain-on-failure` trace
   coverage with no leak.
3. **DB residue from interrupted round-trip specs.** Add a
   `test.afterEach` that PATCHes `workspace_slot_bonus` back to its
   pre-test value via the admin API, so an interrupted run leaves no
   state behind. For local dev this is acceptable to defer (admin can
   reset via UI); for CI it is mandatory.
4. **Tighten the 15s `waitFor` timeout** in `admin-user-detail.spec.ts`
   to ~30s. Cold `next dev` first-paint can take 8–12s on slow CI
   runners — 15s leaves no safety margin.
