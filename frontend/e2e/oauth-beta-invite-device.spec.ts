import type {
  APIRequestContext,
  Browser,
  BrowserContext,
} from "@playwright/test";

import { test, expect, API_URL } from "./fixtures/admin-auth";
import { INVITE_HANDOFF_TEST_IDS as T } from "@/components/auth/invite-handoff.testids";

/**
 * Beta invite through a device sign-in, end to end (#1655).
 *
 * A new person holding an invite starts from a CLI device login. The CLI's
 * link is `/join/<token>?return_to=/device?user_code=<code>`: the invite rides
 * the OAuth login, the signup gate lets them in, and they land back on /device
 * with the code filled in. Without the invite the same person is refused.
 *
 * Needs the `frontend-e2e-oauth` lane: the mock IdP (e2e/mock-idp/server.mjs)
 * behind the backend's OAUTH_GITHUB_* overrides, and ENABLE_BETA_INVITES=true.
 * The gate itself is a runtime setting: each test turns it on in `manual` mode
 * through the admin API and puts the previous value back afterwards.
 *
 * The admin (the inviter) uses the `authed` project's storageState via
 * `context.request`. The invitee gets a fresh browser context with no session
 * and a `mock_idp_gh` cookie that makes the mock IdP return a brand-new GitHub
 * identity, so every run is a real first sign-up.
 *
 * The invite token is a credential: it is never logged here, and assertions
 * compare it without printing it.
 */

const DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code";

interface GateConfig {
  enabled: boolean;
  mode: string;
}

interface DeviceStart {
  clientId: string;
  deviceCode: string;
  userCode: string;
}

/** A GitHub identity no earlier run has used. */
function freshIdentity(tag: string) {
  const sub = String(8_000_000_000 + (Date.now() % 1_000_000_000));
  return {
    sub,
    login: `e2e-${tag}-${sub}`,
    email: `e2e-${tag}-${sub}@example.com`,
    name: `E2E ${tag}`,
  };
}

/** A browser with no session that the mock IdP will know as `identity`. */
async function invitee(
  browser: Browser,
  identity: ReturnType<typeof freshIdentity>,
): Promise<BrowserContext> {
  const context = await browser.newContext({
    storageState: { cookies: [], origins: [] },
  });
  await context.addCookies([
    {
      name: "mock_idp_gh",
      value: Buffer.from(JSON.stringify(identity)).toString("base64url"),
      domain: "localhost",
      path: "/",
    },
  ]);
  return context;
}

/** Turn the gate on in manual mode; returns a function that restores it. */
async function gateOn(api: APIRequestContext): Promise<() => Promise<void>> {
  const url = `${API_URL}/api/v1/admin/signup-gate/config`;
  const before = await api.get(url);
  expect(before.ok(), `GET signup-gate config: ${before.status()}`).toBe(true);
  const previous = (await before.json()) as GateConfig;

  const on = await api.put(url, { data: { enabled: true, mode: "manual" } });
  expect(on.ok(), `PUT signup-gate config: ${on.status()}`).toBe(true);

  return async () => {
    // The write model only accepts `manual`; CI starts from the default
    // (disabled, manual), so only `enabled` needs to go back.
    await api.put(url, {
      data: { enabled: previous.enabled, mode: "manual" },
    });
  };
}

/** What a CLI does first: register as a public client, start a device grant. */
async function startDeviceAuthorization(
  api: APIRequestContext,
): Promise<DeviceStart> {
  const registered = await api.post(`${API_URL}/api/v1/oauth/register`, {
    data: {
      client_name: "Claude Code (e2e)",
      redirect_uris: ["http://localhost:60801/callback"],
      grant_types: ["authorization_code", "refresh_token", DEVICE_GRANT],
    },
  });
  expect(registered.status(), "DCR").toBe(201);
  const { client_id: clientId } = (await registered.json()) as {
    client_id: string;
  };

  const started = await api.post(`${API_URL}/api/v1/oauth/device/authorize`, {
    data: { client_id: clientId },
  });
  expect(started.ok(), `device/authorize: ${started.status()}`).toBe(true);
  const body = (await started.json()) as {
    device_code: string;
    user_code: string;
  };
  return { clientId, deviceCode: body.device_code, userCode: body.user_code };
}

/** One poll of the token endpoint, as the CLI would. */
async function pollToken(api: APIRequestContext, device: DeviceStart) {
  const response = await api.post(`${API_URL}/api/v1/oauth/token`, {
    form: {
      grant_type: DEVICE_GRANT,
      device_code: device.deviceCode,
      client_id: device.clientId,
    },
  });
  return {
    status: response.status(),
    body: (await response.json()) as Record<string, unknown>,
  };
}

test.describe("beta invite through a device sign-in (#1655)", () => {
  test("an invitee signs up from /join and lands back on /device", async ({
    browser,
    context,
  }) => {
    const api = context.request;
    const restoreGate = await gateOn(api);
    try {
      const device = await startDeviceAuthorization(api);

      const created = await api.post(`${API_URL}/api/v1/beta-invites`, {
        data: { label: "e2e device hand-off" },
      });
      expect(
        created.status(),
        "POST /beta-invites (is ENABLE_BETA_INVITES=true?)",
      ).toBe(201);
      const invite = (await created.json()) as { id: string; url: string };
      const token = new URL(invite.url).pathname.split("/").pop() ?? "";

      const guest = await invitee(browser, freshIdentity("invitee"));
      try {
        const page = await guest.newPage();
        const returnTo = `/device?user_code=${device.userCode}`;
        await page.goto(
          `/join/${token}?return_to=${encodeURIComponent(returnTo)}`,
        );

        // Terms first: the provider button waits for it.
        const github = page.getByTestId(T.joinProvider("github"));
        await expect(github).toBeDisabled({ timeout: 30_000 });
        await page.getByTestId(T.joinTerms).check();
        await github.click();

        // mock IdP → backend callback (gate + invite) → back to /device.
        await page.waitForURL(
          (url) =>
            url.pathname === "/device" &&
            url.searchParams.get("user_code") === device.userCode,
          { timeout: 30_000 },
        );
        await page.getByTestId(T.deviceApprove).click({ timeout: 30_000 });
        await expect(page.getByTestId(T.deviceSuccess)).toBeVisible({
          timeout: 15_000,
        });
      } finally {
        await guest.close();
      }

      const polled = await pollToken(api, device);
      expect(polled.status).toBe(200);
      expect(typeof polled.body.access_token).toBe("string");

      const mine = await api.get(`${API_URL}/api/v1/beta-invites/me`);
      expect(mine.ok()).toBe(true);
      const { invites } = (await mine.json()) as {
        invites: { id: string; status: string }[];
      };
      expect(invites.find((i) => i.id === invite.id)?.status).toBe("redeemed");
    } finally {
      await restoreGate();
    }
  });

  test("the same sign-in without an invite is refused by the gate", async ({
    browser,
    context,
  }) => {
    const api = context.request;
    const restoreGate = await gateOn(api);
    try {
      const device = await startDeviceAuthorization(api);

      const guest = await invitee(browser, freshIdentity("uninvited"));
      try {
        const page = await guest.newPage();
        // /device sends a signed-out visitor to /login?return_to=/device?…
        await page.goto(`/device?user_code=${device.userCode}`);
        await page.waitForURL((url) => url.pathname === "/login", {
          timeout: 30_000,
        });
        await page.getByRole("checkbox").first().check({ timeout: 30_000 });
        await page.getByRole("button", { name: /GitHub/ }).click();

        await page.waitForURL((url) => url.pathname === "/signup-blocked", {
          timeout: 30_000,
        });
      } finally {
        await guest.close();
      }

      const polled = await pollToken(api, device);
      expect(polled.status).toBe(400);
      expect(polled.body.error).toBe("authorization_pending");
    } finally {
      await restoreGate();
    }
  });
});
