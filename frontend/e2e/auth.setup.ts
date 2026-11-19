import { test as setup, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import {
  API_URL,
  ADMIN_LOGIN_ID,
  ADMIN_PASSWORD,
  SLOT_OWNER_LOGIN_ID,
  SLOT_OWNER_PASSWORD,
} from "./fixtures/admin-auth";

/**
 * One-time auth setup for the `authed` Playwright project (#959).
 *
 * Runs as the `setup` project (a dependency of `authed` in
 * `playwright.config.ts`). Logs the test admin in via the API **exactly once**
 * and writes the resulting session cookie to `e2e/.auth/admin.json`. Every
 * authed spec then reuses that cookie through the `authed` project's
 * `use.storageState` — no per-test re-login.
 *
 * This is the fix for the `frontend-a11y-authed` flake: the repo enforces
 * single-session-per-user (Issue #114, `delete_user_sessions` on every login),
 * so re-logging-in per test let parallel workers sharing the one `e2e-admin`
 * account clobber each other's session → intermittent 401s. Logging in once
 * removes the race.
 *
 * `trace: "off"` is set for this project in `playwright.config.ts` so the login
 * POST body (which carries E2E_ADMIN_PASSWORD) is never captured in a retained
 * trace artifact.
 */

// Must match `STORAGE_STATE` in playwright.config.ts.
const STORAGE_STATE = path.join(__dirname, ".auth", "admin.json");
// #2919: read by e2e/authed-a11y/workspace-management-contrast.spec.ts.
const SLOT_OWNER_STATE = path.join(__dirname, ".auth", "slot-owner.json");

setup("authenticate as e2e-admin", async ({ request }) => {
  if (!ADMIN_LOGIN_ID || !ADMIN_PASSWORD) {
    throw new Error(
      "E2E_ADMIN_LOGIN_ID and E2E_ADMIN_PASSWORD must be set. " +
        "See frontend/e2e/README.md for setup.",
    );
  }

  const response = await request.post(`${API_URL}/api/v1/auth/login`, {
    data: { login_id: ADMIN_LOGIN_ID, password: ADMIN_PASSWORD },
  });
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

  // The login Set-Cookie lands in the `request` context's cookie jar;
  // persist it (cookies only, no password) for the authed project to load.
  fs.mkdirSync(path.dirname(STORAGE_STATE), { recursive: true });
  await request.storageState({ path: STORAGE_STATE });
});

/**
 * #2919: the second login, for the Workspace management spec. A separate user
 * (its own session, so the single-session rule never touches the admin's) that
 * owns three workspaces with one active slot. Skipped wherever
 * `E2E_SLOT_OWNER_LOGIN_ID` is unset (every lane but `frontend-a11y-authed`).
 */
setup("authenticate as the workspace-slot owner", async ({ request }) => {
  setup.skip(
    !SLOT_OWNER_LOGIN_ID,
    "E2E_SLOT_OWNER_LOGIN_ID is unset: no slot-owner seed in this lane",
  );
  if (!SLOT_OWNER_PASSWORD) {
    throw new Error("E2E_SLOT_OWNER_PASSWORD must be set with E2E_SLOT_OWNER_LOGIN_ID.");
  }

  const response = await request.post(`${API_URL}/api/v1/auth/login`, {
    data: { login_id: SLOT_OWNER_LOGIN_ID, password: SLOT_OWNER_PASSWORD },
  });
  expect(
    response.ok(),
    `slot-owner login failed (${response.status()}): ${await response.text()}`,
  ).toBe(true);
  const body = await response.json();
  if (body.mfa_required) {
    throw new Error("The E2E slot owner must not have MFA enabled.");
  }

  fs.mkdirSync(path.dirname(SLOT_OWNER_STATE), { recursive: true });
  await request.storageState({ path: SLOT_OWNER_STATE });
});

