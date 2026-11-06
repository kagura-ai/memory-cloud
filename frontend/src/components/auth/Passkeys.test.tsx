import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PasskeyLogin from "./PasskeyLogin";
import PasskeySettings from "./PasskeySettings";

const { user, get, post, del, createCredential, getCredential } = vi.hoisted(
  () => ({
    user: {
      id: "u1",
      has_password: true,
      email: "user@example.org",
      auth_provider: "google",
    },
    del: vi.fn(),
    get: vi.fn(),
    post: vi.fn(),
    createCredential: vi.fn(),
    getCredential: vi.fn(),
  }),
);
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user }),
}));
vi.mock("@/lib/api/base", () => ({ apiClient: { get, post, delete: del } }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));

beforeEach(() => {
  vi.stubGlobal("PublicKeyCredential", {
    isConditionalMediationAvailable: () => Promise.resolve(true),
  });
  Object.defineProperty(navigator, "credentials", {
    configurable: true,
    value: { get: getCredential, create: createCredential },
  });
  user.has_password = true;
  user.email = "user@example.org";
  del.mockReset().mockResolvedValue(undefined);
  get.mockReset().mockResolvedValue([]);
  post.mockReset();
  createCredential.mockReset();
  getCredential.mockReset();
});
afterEach(() => vi.unstubAllGlobals());

describe("browser passkeys", () => {
  it("hides controls when WebAuthn is unavailable", () => {
    vi.stubGlobal("PublicKeyCredential", undefined);
    render(
      <>
        <PasskeyLogin enabled conditional returnTo="/" />
        <PasskeySettings onAvailabilityChange={vi.fn()} />
      </>,
    );
    expect(screen.queryByRole("button")).toBeNull();
    expect(get).not.toHaveBeenCalled();
  });

  it.each(["google", "github"])(
    "guides %s users through mailbox proof before removing their only key",
    async (provider) => {
      user.has_password = false;
      user.auth_provider = provider;
      get.mockResolvedValue([
        {
          id: "only",
          name: "Hello",
          created_at: "2026-01-01",
          last_used_at: null,
        },
      ]);
      const page = render(<PasskeySettings onAvailabilityChange={vi.fn()} />);
      const remove = await screen.findByRole("button", { name: "remove" });
      expect(remove).toBeDisabled();
      expect(remove).toHaveAccessibleDescription("onlyKeyRemoval");
      expect(
        screen.getByRole("link", { name: "setupPassword" }),
      ).toHaveAttribute("href", "#profile-password");
      expect(
        screen.queryByRole("button", { name: "confirmGoogle" }),
      ).toBeNull();
      fireEvent.click(remove);
      expect(del).not.toHaveBeenCalled();

      // Setup completion refreshed the provider before returning to Profile.
      user.has_password = true;
      page.rerender(<PasskeySettings onAvailabilityChange={vi.fn()} />);
      fireEvent.change(screen.getByLabelText("password"), {
        target: { value: "new-password" },
      });
      fireEvent.click(remove);
      await waitFor(() =>
        expect(del).toHaveBeenCalledWith("/api/v1/auth/passkeys/only", {
          body: JSON.stringify({ current_password: "new-password" }),
        }),
      );
      expect(await screen.findByRole("button", { name: "add" })).toBeEnabled();
    },
  );

  it("registers the platform result, and lists the key returned by the server", async () => {
    const options = {
      challenge: "YQ",
      rp: { id: "localhost", name: "test" },
      user: { id: "Yg", name: "person", displayName: "Person" },
      pubKeyCredParams: [],
      excludeCredentials: [],
    };
    const key = {
      id: "key",
      name: "Windows Hello",
      created_at: "2026-01-01T00:00:00",
      last_used_at: null,
    };
    post.mockResolvedValueOnce(options).mockResolvedValueOnce(key);
    const bytes = Uint8Array.of(97).buffer;
    createCredential.mockResolvedValue({
      id: "YQ",
      rawId: bytes,
      type: "public-key",
      response: {
        clientDataJSON: bytes,
        attestationObject: bytes,
        getTransports: () => ["internal"],
      },
    });
    render(<PasskeySettings onAvailabilityChange={vi.fn()} />);
    const add = await screen.findByRole("button", { name: "add" });
    await waitFor(() => expect(add).toBeEnabled());
    fireEvent.change(screen.getByLabelText("password"), {
      target: { value: "secret" },
    });
    fireEvent.click(add);
    expect(await screen.findByText(key.name)).toBeVisible();
    expect(post).toHaveBeenNthCalledWith(
      1,
      "/api/v1/auth/passkeys/register/options",
      { current_password: "secret" },
    );
    expect(post.mock.calls[1][1].credential.response.transports).toEqual([
      "internal",
    ]);
    expect(
      createCredential.mock.calls[0][0].publicKey.challenge.byteLength,
    ).toBe(1);
  });

  it("waits for terms before offering autofill, and requests conditional mediation", async () => {
    post.mockResolvedValue({ challenge: "YQ", rpId: "localhost" });
    getCredential.mockImplementation(() => new Promise(() => {}));
    const page = render(
      <PasskeyLogin
        enabled={false}
        conditional
        terms="1"
        returnTo="/profile"
      />,
    );
    expect(
      await screen.findByRole("button", { name: "signIn" }),
    ).toBeDisabled();
    expect(getCredential).not.toHaveBeenCalled();
    page.rerender(
      <PasskeyLogin enabled conditional terms="1" returnTo="/profile" />,
    );
    await waitFor(() => expect(getCredential).toHaveBeenCalledTimes(1));
    const options = getCredential.mock.calls[0][0];
    expect(options.mediation).toBe("conditional");
    page.unmount();
    expect(options.signal.aborted).toBe(true);
  });
});
