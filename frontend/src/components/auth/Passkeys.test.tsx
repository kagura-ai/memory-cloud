import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/base";
import PasskeyLogin from "./PasskeyLogin";
import PasskeySettings from "./PasskeySettings";

const {
  user,
  get,
  post,
  patch,
  del,
  createCredential,
  getCredential,
  setupRequest,
  refetchUser,
  toast,
} = vi.hoisted(() => ({
  user: {
    id: "u1",
    has_password: true,
    email: "user@example.org",
    auth_provider: "google",
  },
  del: vi.fn(),
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  createCredential: vi.fn(),
  getCredential: vi.fn(),
  setupRequest: vi.fn(),
  refetchUser: vi.fn(),
  toast: vi.fn(),
}));
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user, refetchUser }),
}));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));
vi.mock("@/lib/api/base", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/base")>()),
  apiClient: { get, post, patch, delete: del },
}));
vi.mock("@/lib/auth/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth/auth")>()),
  requestPasswordSetup: (...args: unknown[]) => setupRequest(...args),
}));
// Keys of the card's own namespace render bare; any other namespace keeps its
// prefix, so a test can tell reused copy from passkey copy.
vi.mock("next-intl", () => ({
  useTranslations:
    (namespace: string) => (key: string, values?: Record<string, unknown>) => {
      const text = namespace === "passkeys" ? key : `${namespace}.${key}`;
      return values && "email" in values ? `${text}|${values.email}` : text;
    },
}));

beforeEach(() => {
  vi.stubGlobal("PublicKeyCredential", {
    isConditionalMediationAvailable: () => Promise.resolve(true),
  });
  Object.defineProperty(navigator, "credentials", {
    configurable: true,
    value: { get: getCredential, create: createCredential },
  });
  user.id = "u1";
  user.has_password = true;
  user.email = "user@example.org";
  user.auth_provider = "google";
  del.mockReset().mockResolvedValue(undefined);
  get.mockReset().mockResolvedValue([]);
  post.mockReset();
  patch.mockReset();
  createCredential.mockReset();
  getCredential.mockReset();
  setupRequest.mockReset();
  refetchUser.mockReset();
  toast.mockReset();
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
        screen.getByRole("button", { name: "setupPassword" }),
      ).toBeEnabled();
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

// #2699: the card shows its own copy, chosen by status, by what was being
// done and by the browser's DOMException name — never the server's (English)
// prose or the browser's.
const serverProse =
  "Verify with your current password or another passkey, or sign in again";
const apiError = (status: number, code: string, message = serverProse) =>
  new ApiError({ status, error: code, message });
const keyRows = (count: number) =>
  Array.from({ length: count }, (_, i) => ({
    id: `k${i}`,
    name: `Key ${i}`,
    created_at: "2026-01-01T00:00:00",
    last_used_at: null,
  }));
const creationOptions = {
  challenge: "YQ",
  rp: { id: "localhost", name: "test" },
  user: { id: "Yg", name: "person", displayName: "Person" },
  pubKeyCredParams: [],
  excludeCredentials: [],
};
const bytes = Uint8Array.of(97).buffer;
const platformCredential = {
  id: "YQ",
  rawId: bytes,
  type: "public-key",
  response: {
    clientDataJSON: bytes,
    attestationObject: bytes,
    getTransports: () => ["internal"],
  },
};

async function renderCard() {
  render(<PasskeySettings onAvailabilityChange={vi.fn()} />);
  const add = await screen.findByRole("button", { name: "add" });
  await waitFor(() => expect(add).toBeEnabled());
  return add;
}

function typePassword(value?: string) {
  if (value === undefined) return;
  fireEvent.change(screen.getByLabelText("password"), { target: { value } });
}

// A failed action is one destructive toast (frontend.md "Error Surface"), and
// nothing inline; its description is the message key.
async function toastedError() {
  await waitFor(() => expect(toast).toHaveBeenCalledTimes(1));
  const [payload] = toast.mock.calls[0];
  expect(payload).toMatchObject({
    title: "common.error",
    variant: "destructive",
  });
  expect(screen.queryByRole("alert")).toBeNull();
  return payload.description;
}

async function addPasskey(typed?: string) {
  const add = await renderCard();
  typePassword(typed);
  fireEvent.click(add);
  return toastedError();
}

async function removeFirstPasskey(typed?: string) {
  await renderCard();
  typePassword(typed);
  fireEvent.click(screen.getAllByRole("button", { name: "remove" })[0]);
  return toastedError();
}

describe("passkey card errors (#2699)", () => {
  it.each([
    ["a typed password that did not match", true, "wrong", "errors.wrongPassword"],
    ["no typed password, with one set", true, undefined, "errors.passwordRequired"],
    ["no password and no recent sign-in", false, undefined, "errors.signInAgain"],
  ])(
    "explains the proof refusal (403) when adding, for %s",
    async (_, hasPassword, typed, message) => {
      user.has_password = hasPassword;
      post.mockRejectedValue(apiError(403, "AUTH-101"));
      expect(await addPasskey(typed)).toBe(message);
      expect(screen.queryByText(serverProse)).toBeNull();
    },
  );

  it.each([
    ["a typed password that did not match", "wrong", "errors.wrongPassword"],
    ["no typed password", undefined, "errors.removeProof"],
  ])(
    "explains the proof refusal (403) when removing, for %s",
    async (_, typed, message) => {
      get.mockResolvedValue(keyRows(1));
      del.mockRejectedValue(apiError(403, "AUTH-101"));
      expect(await removeFirstPasskey(typed)).toBe(message);
    },
  );

  it("asks the user to wait after too many attempts (429)", async () => {
    post.mockRejectedValue(
      apiError(429, "RATE-001", "Too many verification attempts"),
    );
    expect(await addPasskey("secret")).toBe("errors.tooManyAttempts");
  });

  it("names the passkey limit when a full account adds another (409)", async () => {
    get.mockResolvedValue(keyRows(10));
    post.mockRejectedValue(apiError(409, "RES-002", "Passkey limit reached"));
    expect(await addPasskey("secret")).toBe("errors.limitReached");
  });

  it("asks for a reload when the server already holds the new key (409)", async () => {
    get.mockResolvedValue(keyRows(1));
    post
      .mockResolvedValueOnce(creationOptions)
      .mockRejectedValueOnce(
        apiError(409, "RES-002", "Passkey already registered"),
      );
    createCredential.mockResolvedValue(platformCredential);
    expect(await addPasskey("secret")).toBe("errors.changed");
  });

  it("asks for a reload when the other passkey is gone (409)", async () => {
    user.has_password = false;
    get.mockResolvedValue(keyRows(2));
    post.mockRejectedValue(
      apiError(409, "RES-002", "No other passkey is available"),
    );
    expect(await removeFirstPasskey()).toBe("errors.changed");
    expect(del).not.toHaveBeenCalled();
  });

  it("says the prompt was closed when creating a key is cancelled", async () => {
    post.mockResolvedValue(creationOptions);
    createCredential.mockRejectedValue(
      new DOMException(
        "The operation either timed out or was not allowed.",
        "NotAllowedError",
      ),
    );
    expect(await addPasskey("secret")).toBe("errors.cancelled");
  });

  it("says the prompt was closed when confirming with another key is cancelled", async () => {
    user.has_password = false;
    get.mockResolvedValue(keyRows(2));
    post.mockResolvedValue({ challenge: "YQ", rpId: "localhost" });
    getCredential.mockRejectedValue(
      new DOMException("The request is not allowed.", "NotAllowedError"),
    );
    expect(await removeFirstPasskey()).toBe("errors.cancelled");
    expect(del).not.toHaveBeenCalled();
  });

  it("says this device already holds a key (InvalidStateError)", async () => {
    post.mockResolvedValue(creationOptions);
    createCredential.mockRejectedValue(
      new DOMException("Credential already registered.", "InvalidStateError"),
    );
    expect(await addPasskey("secret")).toBe("errors.alreadyRegistered");
  });

  it("reports an unverified passkey or an expired request (401)", async () => {
    post
      .mockResolvedValueOnce(creationOptions)
      .mockRejectedValueOnce(
        apiError(401, "AUTH-001", "Invalid or expired passkey challenge"),
      );
    createCredential.mockResolvedValue(platformCredential);
    expect(await addPasskey("secret")).toBe("errors.verifyFailed");
  });

  it.each([
    ["a network failure", new TypeError("Failed to fetch")],
    ["a server error", apiError(500, "INT-001", "Internal server error")],
    ["another browser refusal", new DOMException("Bad RP ID", "SecurityError")],
  ])("falls back to the generic message for %s", async (_, failure) => {
    post.mockRejectedValue(failure);
    expect(await addPasskey("secret")).toBe("error");
  });

  it("asks for a reload when a renamed key is gone (404)", async () => {
    get.mockResolvedValue(keyRows(1));
    patch.mockRejectedValue(apiError(404, "RES-001", "Passkey not found"));
    await renderCard();
    fireEvent.click(screen.getByRole("button", { name: "rename" }));
    fireEvent.change(screen.getByLabelText("name"), {
      target: { value: "Laptop" },
    });
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    expect(await toastedError()).toBe("errors.changed");
  });

  it("shows its own copy in a banner when the list cannot be loaded", async () => {
    get.mockRejectedValue(apiError(503, "SVC-001", "Service unavailable"));
    render(<PasskeySettings onAvailabilityChange={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toBe(
      "errors.load",
    );
    expect(screen.getByRole("button", { name: "add" })).toBeDisabled();
    expect(toast).not.toHaveBeenCalled();
  });
});

describe("setting up a password from the passkey card (#2699)", () => {
  beforeEach(() => {
    user.has_password = false;
    user.auth_provider = "github";
    get.mockResolvedValue(keyRows(1));
  });

  it("sends the setup email from the card and says where it went", async () => {
    let finish = () => {};
    setupRequest.mockReturnValue(
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
    );
    render(<PasskeySettings onAvailabilityChange={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "setupPassword" }),
    );
    expect(
      screen.getByRole("button", { name: "passwordSettings.setupSending" }),
    ).toBeDisabled();
    expect(setupRequest).toHaveBeenCalledTimes(1);
    finish();
    expect((await screen.findByRole("status")).textContent).toBe(
      "passwordSettings.setupSent|user@example.org",
    );
    expect(screen.queryByRole("button", { name: /setup/ })).toBeNull();
    expect(toast).not.toHaveBeenCalled();
  });

  it("offers the email again after switching to another account", async () => {
    setupRequest.mockResolvedValue(undefined);
    const page = render(<PasskeySettings onAvailabilityChange={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "setupPassword" }),
    );
    await screen.findByRole("status");
    user.id = "u2";
    page.rerender(<PasskeySettings onAvailabilityChange={vi.fn()} />);
    expect(
      await screen.findByRole("button", { name: "setupPassword" }),
    ).toBeEnabled();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it.each([
    [429, "passwordSettings.tooManyRequests"],
    [503, "passwordSettings.setupError"],
  ])(
    "toasts a refused send (%i) and lets the user retry",
    async (status, message) => {
      setupRequest.mockRejectedValue(apiError(status, "X", "English prose"));
      render(<PasskeySettings onAvailabilityChange={vi.fn()} />);
      fireEvent.click(
        await screen.findByRole("button", { name: "setupPassword" }),
      );
      expect(await toastedError()).toBe(message);
      expect(
        screen.getByRole("button", { name: "setupPassword" }),
      ).toBeEnabled();
      expect(screen.queryByRole("status")).toBeNull();
    },
  );

  it("refreshes the user when a password was already set elsewhere (409)", async () => {
    setupRequest.mockRejectedValue(apiError(409, "RES-002", "Already set"));
    refetchUser.mockResolvedValue({ ...user, has_password: true });
    render(<PasskeySettings onAvailabilityChange={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "setupPassword" }),
    );
    await waitFor(() => expect(refetchUser).toHaveBeenCalledTimes(1));
    expect(toast).not.toHaveBeenCalled();
  });

  it("says so when that refresh fails too (409, no user back)", async () => {
    setupRequest.mockRejectedValue(apiError(409, "RES-002", "Already set"));
    refetchUser.mockResolvedValue(undefined);
    render(<PasskeySettings onAvailabilityChange={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "setupPassword" }),
    );
    expect(await toastedError()).toBe("errors.changed");
  });
});
