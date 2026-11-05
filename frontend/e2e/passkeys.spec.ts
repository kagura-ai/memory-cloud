import { test, expect, ADMIN_PASSWORD, API_URL } from "./fixtures/admin-auth";

// This flow signs the shared admin out everywhere and enters its password.
// Run in its own project after other authenticated specs and never record a trace.
test.use({ trace: "off", locale: "en-US" });

test("register, sign in, rename and remove a platform passkey", async ({
  page,
  context,
}) => {
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  const device = await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  try {
    await page.goto("/profile");
    await page.locator("#passkey-password").fill(ADMIN_PASSWORD);
    await page
      .getByRole("button", { name: /add a passkey|パスキーを追加/i })
      .click();
    await expect(
      page.getByText("Platform passkey", { exact: true }),
    ).toBeVisible();

    await context.clearCookies();
    await page.goto("/login");
    await page.getByRole("checkbox").first().check();
    await page
      .getByRole("button", {
        name: /sign in with a passkey|パスキーでサインイン/i,
      })
      .click();
    await expect(page).toHaveURL(/\/workspace\//);
    await page.goto("/profile");
    const card = page.locator("#profile-passkeys");
    await card.getByRole("button", { name: /rename|名前を変更/i }).click();
    await card
      .getByRole("textbox", { name: /passkey name|パスキー名/i })
      .fill("Virtual platform key");
    await card.getByRole("button", { name: /save|保存/i }).click();
    const renamed = page.getByText("Virtual platform key", { exact: true });
    await expect(renamed).toBeVisible();
    await page.locator("#passkey-password").fill(ADMIN_PASSWORD);
    await page
      .locator("#profile-passkeys")
      .getByRole("button", { name: /remove|削除/i })
      .click();
    await expect(renamed).toHaveCount(0);
    const remaining = await context.request.get(
      API_URL + "/api/v1/auth/passkeys",
    );
    expect(
      (await remaining.json()).some(
        (key: { name: string }) => key.name === "Virtual platform key",
      ),
    ).toBe(false);
  } finally {
    await cdp.send("WebAuthn.removeVirtualAuthenticator", {
      authenticatorId: device.authenticatorId,
    });
    await cdp.detach();
  }
});
