/**
 * Tests for the profile page (Issues #514 + #515).
 *
 * Covers:
 *   - getSignInMethodLabel helper (4+1 branches)            — #514
 *   - render: "Sign-in method" Input shows the right label  — #514
 *   - getProviderName / getRefreshProviderName helpers      — #515 / #1751
 *   - render: refresh button visible for google/github only — #515
 *   - click: POST /me/refresh-oauth → window.location set   — #515
 *   - click error: 429 → "rate limited" toast               — #515
 *   - search-param effect: refreshed=1 → success toast      — #515
 *   - search-param effect: error=refresh_* → destructive    — #515
 *   - sign-in method lists linked providers + email/password — #1751
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { resetConsumedSearchParams } from "@/hooks/useConsumeSearchParams";

import ProfilePage from "./page";
import {
  getSignInMethodLabel,
  getProviderName,
  getRefreshProviderName,
} from "./signInLabels";
import { hasPasswordSignIn } from "@/lib/auth/auth";

// ---------- Mocks ------------------------------------------------------------

const stableTranslator = (key: string, values?: Record<string, unknown>) => {
  // Translator stub: surfaces the key plus any interpolated provider arg
  // so the test can assert on both the i18n key choice AND the value.
  if (values && "provider" in values) return `${key}|${values.provider}`;
  // #1751: a readable separator so joined sign-in methods stay assertable.
  if (key === "signInMethodSeparator") return ", ";
  return key;
};
vi.mock("next-intl", () => ({
  useTranslations: (_namespace: string) => stableTranslator,
  useLocale: () => "en",
}));

// The account-deletion danger zone is a separately-tested unit
// (DeleteAccountSection.test.tsx); stub it here so the profile page test
// stays focused on profile concerns and isn't coupled to the erasure flow's
// own mount-time fetch and hooks.
vi.mock("@/components/account/DeleteAccountSection", () => ({
  DeleteAccountSection: () => null,
}));
// #1678: the Password section has its own suite (PasswordSettings.test.tsx)
// and its own providers fetch.
// Both sections are stubbed down to the wiring between them: Connected
// Accounts reports a change, the Password section re-reads the providers.
vi.mock("@/components/auth/PasswordSettings", () => ({
  default: ({ providersVersion }: { providersVersion?: number }) => (
    <div data-testid="password-settings-stub">{String(providersVersion)}</div>
  ),
}));
// #1784: the Linked accounts section has its own suite and its own fetch.
vi.mock("@/components/auth/LinkedAccounts", () => ({
  default: () => null,
}));
vi.mock("@/components/auth/ConnectedAccounts", () => ({
  default: ({ onProvidersChanged }: { onProvidersChanged?: () => void }) => (
    <button type="button" onClick={() => onProvidersChanged?.()}>
      connected-accounts-stub
    </button>
  ),
}));

// AuthContext mock — flipped per test via mockUser.
let mockUser: {
  id: string;
  email: string;
  name: string;
  role?: string;
  timezone?: string;
  auth_method?: "password" | "oauth";
  auth_provider?: "google" | "github" | null;
  has_password?: boolean;
} | null = null;
const mockRefetchUser = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: mockUser,
    refetchUser: mockRefetchUser,
  }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const { mockApiGet, mockApiPost, mockApiPut, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number;
    constructor(status: number, message = "fake-error") {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }
  return {
    mockApiGet: vi.fn(),
    mockApiPost: vi.fn(),
    mockApiPut: vi.fn(),
    FakeApiError,
  };
});
vi.mock("@/lib/api/base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockApiGet(...args),
    post: (...args: unknown[]) => mockApiPost(...args),
    put: (...args: unknown[]) => mockApiPut(...args),
  },
  ApiError: FakeApiError,
}));

// next/navigation: useRouter / useSearchParams
const mockRouterReplace = vi.fn();
let mockSearchParamsValue = new URLSearchParams("");
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: mockRouterReplace,
  }),
  useSearchParams: () => ({
    get: (key: string) => mockSearchParamsValue.get(key),
  }),
}));

beforeEach(() => {
  // #1532: the hook remembers consumed params across remounts (module-level);
  // forget them so one case's URL params cannot suppress the next case's toast.
  resetConsumedSearchParams();
  mockUser = null;
  mockRefetchUser.mockClear();
  mockToast.mockClear();
  mockApiPost.mockReset();
  // #1751: the providers fetch fails by default, so the page falls back to
  // `auth_provider` and the pre-#1751 cases keep their expectations.
  mockApiGet.mockReset();
  mockApiGet.mockRejectedValue(new FakeApiError(500));
  mockRouterReplace.mockClear();
  mockSearchParamsValue = new URLSearchParams("");
});

// ---------- getSignInMethodLabel (#514) -------------------------------------

describe("getSignInMethodLabel", () => {
  it("returns Google label for OAuth + google provider", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: "google" },
        stableTranslator,
      ),
    ).toBe("signInMethodGoogle");
  });

  it("returns GitHub label for OAuth + github provider", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: "github" },
        stableTranslator,
      ),
    ).toBe("signInMethodGitHub");
  });

  it("returns the email/password label for an unlinked password account", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "password", auth_provider: null },
        stableTranslator,
      ),
    ).toBe("signInMethodEmailPassword");
  });

  it("returns Other label for legacy OAuth user with null provider", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: null },
        stableTranslator,
      ),
    ).toBe("signInMethodOther");
  });

  it("returns Other label when both fields are undefined (defensive)", () => {
    expect(getSignInMethodLabel({}, stableTranslator)).toBe(
      "signInMethodOther",
    );
  });
});

// ---------- getSignInMethodLabel with a password + linked providers (#1751) -

describe("getSignInMethodLabel — email and password (#1751)", () => {
  it("lists Google and email/password for an OAuth user who set a password", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: "google", has_password: true },
        stableTranslator,
        ["google"],
      ),
    ).toBe("signInMethodGoogle, signInMethodEmailPassword");
  });

  it("shows only email/password once every OAuth provider is unlinked", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: null, has_password: true },
        stableTranslator,
        [],
      ),
    ).toBe("signInMethodEmailPassword");
  });

  it("lists every linked provider, Google first, whatever auth_provider says", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: "github", has_password: false },
        stableTranslator,
        ["github", "google"],
      ),
    ).toBe("signInMethodGoogle, signInMethodGitHub");
  });

  it("falls back to auth_provider until the providers are loaded", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: "google", has_password: true },
        stableTranslator,
        null,
      ),
    ).toBe("signInMethodGoogle, signInMethodEmailPassword");
  });

  it("keeps a single password label for an unlinked password account", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "password", auth_provider: null, has_password: true },
        stableTranslator,
        [],
      ),
    ).toBe("signInMethodEmailPassword");
  });

  it("names the linked provider on a password-origin account (#1884)", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "password", has_password: true },
        stableTranslator,
        ["google"],
      ),
    ).toBe("signInMethodGoogle, signInMethodEmailPassword");
  });

  it("drops the password from a password-origin account once it is removed (#1884)", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "password", has_password: false },
        stableTranslator,
        ["google"],
      ),
    ).toBe("signInMethodGoogle");
  });

  it("names only known providers; an unknown one alone reads Other", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: null, has_password: false },
        stableTranslator,
        ["microsoft"],
      ),
    ).toBe("signInMethodOther");
  });

  it("uses the translated separator", () => {
    const ja = (key: string) => (key === "signInMethodSeparator" ? "、" : key);
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: "google", has_password: true },
        ja,
        ["google"],
      ),
    ).toBe("signInMethodGoogle、signInMethodEmailPassword");
  });
});

describe("hasPasswordSignIn (#1751)", () => {
  it("trusts has_password when the backend sends it", () => {
    expect(hasPasswordSignIn({ auth_method: "oauth", has_password: true })).toBe(true);
    expect(hasPasswordSignIn({ auth_method: "password", has_password: false })).toBe(false);
  });

  it("falls back to auth_method=password for an older backend", () => {
    expect(hasPasswordSignIn({ auth_method: "password" })).toBe(true);
    expect(hasPasswordSignIn({ auth_method: "oauth" })).toBe(false);
    expect(hasPasswordSignIn(null)).toBe(false);
  });
});

// ---------- getProviderName (#515) ------------------------------------------

describe("getProviderName", () => {
  it("returns the i18n-resolved Google label for OAuth + google", () => {
    // Helper now routes through t() so brand display can be localized
    // (Copilot loop 3 #4). Stub translator surfaces the i18n key for
    // assertion symmetry with the existing fixture pattern.
    expect(
      getProviderName(
        { auth_method: "oauth", auth_provider: "google" },
        stableTranslator,
      ),
    ).toBe("signInMethodGoogle");
  });

  it("returns the i18n-resolved GitHub label for OAuth + github", () => {
    expect(
      getProviderName(
        { auth_method: "oauth", auth_provider: "github" },
        stableTranslator,
      ),
    ).toBe("signInMethodGitHub");
  });

  it("returns null for password user (no IdP to refresh from)", () => {
    expect(
      getProviderName(
        { auth_method: "password", auth_provider: null },
        stableTranslator,
      ),
    ).toBeNull();
  });

  it("returns null for legacy OAuth user with null provider", () => {
    // Pre-#361 — backend would 400 anyway. UI hides the button.
    expect(
      getProviderName(
        { auth_method: "oauth", auth_provider: null },
        stableTranslator,
      ),
    ).toBeNull();
  });

  it("returns null when both fields are undefined", () => {
    expect(getProviderName({}, stableTranslator)).toBeNull();
  });
});

// ---------- getRefreshProviderName (#515 / #1751) ---------------------------

describe("getRefreshProviderName", () => {
  const google = { auth_method: "oauth" as const, auth_provider: "google" as const };

  it("names auth_provider while it is linked", () => {
    expect(getRefreshProviderName(google, stableTranslator, ["google"])).toBe(
      "signInMethodGoogle",
    );
  });

  it("fails closed while the linked providers are unknown", () => {
    expect(getRefreshProviderName(google, stableTranslator, null)).toBeNull();
  });

  it("returns null once auth_provider is no longer linked", () => {
    expect(getRefreshProviderName(google, stableTranslator, ["github"])).toBeNull();
  });

  it("returns null for a password account", () => {
    expect(
      getRefreshProviderName(
        { auth_method: "password", auth_provider: null },
        stableTranslator,
        [],
      ),
    ).toBeNull();
  });
});

// ---------- ProfilePage render: sign-in method (#514) -----------------------

describe("ProfilePage — sign-in method section (#514)", () => {
  it("shows the Google label for an OAuth + google user", () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
    };

    render(<ProfilePage />);

    const input = screen.getByLabelText("signInMethod") as HTMLInputElement;
    expect(input.value).toBe("signInMethodGoogle");
    expect(input.disabled).toBe(true);
  });

  it("shows the Password label for a password-auth user", () => {
    mockUser = {
      id: "u-2",
      email: "u@example.com",
      name: "Test",
      auth_method: "password",
      auth_provider: null,
    };

    render(<ProfilePage />);

    const input = screen.getByLabelText("signInMethod") as HTMLInputElement;
    expect(input.value).toBe("signInMethodEmailPassword");
  });
});

// ---------- ProfilePage render: providers + password (#1751) ----------------

describe("ProfilePage — sign-in method from linked providers (#1751)", () => {
  it("shows Google and email/password once the providers load", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
      has_password: true,
    };
    mockApiGet.mockResolvedValue({ providers: [{ provider: "google" }] });

    render(<ProfilePage />);

    await waitFor(() =>
      expect(
        (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
      ).toBe("signInMethodGoogle, signInMethodEmailPassword"),
    );
    expect(mockApiGet).toHaveBeenCalledWith("/api/v1/me/account/providers");
  });

  it("shows email/password and hides the refresh block with no provider linked", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: null,
      has_password: true,
    };
    mockApiGet.mockResolvedValue({ providers: [] });

    render(<ProfilePage />);

    await waitFor(() =>
      expect(
        (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
      ).toBe("signInMethodEmailPassword"),
    );
    expect(screen.queryByText(/refreshFromIdP\|/)).toBeNull();
  });

  it("hides the refresh block when auth_provider is no longer linked", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
      has_password: true,
    };
    mockApiGet.mockResolvedValue({ providers: [{ provider: "github" }] });

    render(<ProfilePage />);

    await waitFor(() =>
      expect(
        (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
      ).toBe("signInMethodGitHub, signInMethodEmailPassword"),
    );
    expect(screen.queryByText(/refreshFromIdP\|/)).toBeNull();
  });

  it("hides the refresh block for a provider linked later (auth_provider unset)", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: null,
      has_password: true,
    };
    mockApiGet.mockResolvedValue({ providers: [{ provider: "google" }] });

    render(<ProfilePage />);

    await waitFor(() =>
      expect(
        (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
      ).toBe("signInMethodGoogle, signInMethodEmailPassword"),
    );
    expect(screen.queryByText(/refreshFromIdP\|/)).toBeNull();
  });

  it("falls back to auth_provider when a refetch fails", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "github",
      has_password: false,
    };
    mockApiGet.mockResolvedValue({
      providers: [{ provider: "google" }, { provider: "github" }],
    });

    render(<ProfilePage />);
    await waitFor(() =>
      expect(
        (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
      ).toBe("signInMethodGoogle, signInMethodGitHub"),
    );

    mockApiGet.mockRejectedValue(new FakeApiError(500));
    fireEvent.click(screen.getByText("connected-accounts-stub"));

    await waitFor(() =>
      expect(
        (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
      ).toBe("signInMethodGitHub"),
    );
    // The label falls back; the refresh action fails closed.
    expect(screen.queryByText(/refreshFromIdP\|/)).toBeNull();
  });

  it("does not show one user's providers to the next user", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
      has_password: false,
    };
    mockApiGet.mockResolvedValue({
      providers: [{ provider: "google" }, { provider: "github" }],
    });
    const { rerender } = render(<ProfilePage />);
    await waitFor(() =>
      expect(
        (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
      ).toBe("signInMethodGoogle, signInMethodGitHub"),
    );

    mockApiGet.mockReturnValue(new Promise(() => {}));
    mockUser = {
      id: "u-2",
      email: "v@example.com",
      name: "Other",
      auth_method: "oauth",
      auth_provider: "github",
      has_password: false,
    };
    rerender(<ProfilePage />);

    expect(
      (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
    ).toBe("signInMethodGitHub");
  });

  it("ignores a providers response that arrives after a newer one", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
      has_password: true,
    };
    let resolveStale: (v: unknown) => void = () => {};
    mockApiGet.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStale = resolve;
      }),
    );
    render(<ProfilePage />);

    mockApiGet.mockResolvedValueOnce({ providers: [] });
    fireEvent.click(screen.getByText("connected-accounts-stub"));
    await waitFor(() =>
      expect(
        (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
      ).toBe("signInMethodEmailPassword"),
    );

    resolveStale({ providers: [{ provider: "google" }] });
    await new Promise((r) => setTimeout(r, 0));
    expect(
      (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
    ).toBe("signInMethodEmailPassword");
  });

  it("updates the field when a password is added (user refetched)", () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
      has_password: false,
    };
    const { rerender } = render(<ProfilePage />);
    expect(
      (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
    ).toBe("signInMethodGoogle");

    mockUser = { ...mockUser, has_password: true };
    rerender(<ProfilePage />);

    expect(
      (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
    ).toBe("signInMethodGoogle, signInMethodEmailPassword");
  });

  it("re-reads the providers and the user after Connected Accounts changes them", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
      has_password: true,
    };
    mockApiGet.mockResolvedValue({ providers: [{ provider: "google" }] });

    render(<ProfilePage />);
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(1));

    mockApiGet.mockResolvedValue({ providers: [] });
    fireEvent.click(screen.getByText("connected-accounts-stub"));

    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(2));
    expect(mockRefetchUser).toHaveBeenCalled();
    await waitFor(() =>
      expect(
        (screen.getByLabelText("signInMethod") as HTMLInputElement).value,
      ).toBe("signInMethodEmailPassword"),
    );
  });
});

// ---------- ProfilePage render: refresh button (#515) -----------------------

describe("ProfilePage — refresh-from-IdP button visibility (#515)", () => {
  // #1751: the refresh block needs the loaded providers to include
  // auth_provider, so serve the user's own provider as linked.
  beforeEach(() => {
    mockApiGet.mockImplementation(async () => ({
      providers: mockUser?.auth_provider
        ? [{ provider: mockUser.auth_provider }]
        : [],
    }));
  });

  it("renders the refresh button for an OAuth + google user", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
    };
    render(<ProfilePage />);
    // Translator interpolates {provider} → label is "key|Google".
    expect(await screen.findByText("refreshFromIdP|signInMethodGoogle")).toBeTruthy();
    expect(
      await screen.findByRole("button", {
        name: /refreshFromIdPButton\|signInMethodGoogle/,
      }),
    ).toBeTruthy();
  });

  it("renders the refresh button for an OAuth + github user", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "github",
    };
    render(<ProfilePage />);
    expect(await screen.findByText("refreshFromIdP|signInMethodGitHub")).toBeTruthy();
  });

  it("does NOT render the refresh button for a password user", async () => {
    mockUser = {
      id: "u-2",
      email: "u@example.com",
      name: "Test",
      auth_method: "password",
      auth_provider: null,
    };
    render(<ProfilePage />);
    await waitFor(() => expect(mockApiGet).toHaveBeenCalled());
    expect(screen.queryByText(/refreshFromIdP\|/)).toBeNull();
  });

  it("does NOT render the refresh button for a legacy OAuth user (null provider)", async () => {
    mockUser = {
      id: "u-3",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: null,
    };
    render(<ProfilePage />);
    await waitFor(() => expect(mockApiGet).toHaveBeenCalled());
    expect(screen.queryByText(/refreshFromIdP\|/)).toBeNull();
  });
});

// ---------- ProfilePage refresh-button click flow (#515) --------------------

describe("ProfilePage — refresh button click (#515)", () => {
  // #1751: the refresh block needs the loaded providers to include
  // auth_provider, so serve the user's own provider as linked.
  beforeEach(() => {
    mockApiGet.mockImplementation(async () => ({
      providers: mockUser?.auth_provider
        ? [{ provider: mockUser.auth_provider }]
        : [],
    }));
  });

  // Restore window.location after every test in this block so the patched
  // proxy can't leak into other test files in the same vitest worker
  // (Copilot review #2: shared-worker state contamination is a real
  // contributor to the suite's existing parallel-flake surface area).
  let originalLocationDescriptor: PropertyDescriptor | undefined;
  beforeEach(() => {
    originalLocationDescriptor = Object.getOwnPropertyDescriptor(
      window,
      "location",
    );
  });
  afterEach(() => {
    if (originalLocationDescriptor) {
      Object.defineProperty(window, "location", originalLocationDescriptor);
    }
  });

  it("on click POSTs to /me/refresh-oauth and redirects to authorization_url", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
    };
    mockApiPost.mockResolvedValueOnce({
      authorization_url: "https://accounts.google.com/o/oauth2/auth?...",
      state: "abc",
    });

    // Mock window.location.href setter — jsdom's default doesn't trigger
    // navigation, but a Proxy lets us spy on the assignment. The
    // descriptor is captured in beforeEach above and restored in
    // afterEach so this stub does not survive the test.
    const hrefSetter = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      value: new Proxy(originalLocation, {
        set(target, prop, value) {
          if (prop === "href") {
            hrefSetter(value);
            (target as unknown as Record<string, unknown>).href = value;
            return true;
          }
          (target as unknown as Record<string, unknown>)[prop as string] =
            value;
          return true;
        },
      }),
      writable: true,
      configurable: true,
    });

    render(<ProfilePage />);

    const button = await screen.findByRole("button", {
      name: /refreshFromIdPButton\|signInMethodGoogle/,
    });
    fireEvent.click(button);

    await waitFor(() => {
      expect(mockApiPost).toHaveBeenCalledWith("/api/v1/me/refresh-oauth", {});
    });
    await waitFor(() => {
      expect(hrefSetter).toHaveBeenCalledWith(
        "https://accounts.google.com/o/oauth2/auth?...",
      );
    });
  });

  it("on 429 surfaces a destructive rate-limited toast and re-enables the button", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "github",
    };
    mockApiPost.mockRejectedValueOnce(new FakeApiError(429));

    render(<ProfilePage />);

    const button = await screen.findByRole("button", {
      name: /refreshFromIdPButton\|signInMethodGitHub/,
    });
    fireEvent.click(button);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
    const lastToast = mockToast.mock.calls[mockToast.mock.calls.length - 1][0];
    expect(lastToast.variant).toBe("destructive");
    expect(lastToast.description).toBe(
      "refreshFromIdPErrorRateLimited|signInMethodGitHub",
    );
  });

  it("on generic failure surfaces the generic error toast", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
    };
    mockApiPost.mockRejectedValueOnce(new Error("boom"));

    render(<ProfilePage />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: /refreshFromIdPButton\|signInMethodGoogle/,
      }),
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
    const lastToast = mockToast.mock.calls[mockToast.mock.calls.length - 1][0];
    expect(lastToast.variant).toBe("destructive");
    expect(lastToast.description).toBe(
      "refreshFromIdPErrorGeneric|signInMethodGoogle",
    );
  });
});

// ---------- ProfilePage post-callback search-param effect (#515) ------------

describe("ProfilePage — post-callback search-param handling (#515)", () => {
  it("on ?refreshed=1 shows a success toast, refetches user, and strips the param", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
    };
    mockSearchParamsValue = new URLSearchParams("refreshed=1");

    render(<ProfilePage />);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
    const successToast = mockToast.mock.calls[0][0];
    expect(successToast.variant).toBeUndefined(); // success = default variant
    expect(successToast.title).toBe("refreshFromIdPSuccess|signInMethodGoogle");
    expect(mockRefetchUser).toHaveBeenCalled();
    expect(mockRouterReplace).toHaveBeenCalledWith("/profile");
  });

  it("on ?error=refresh_user_mismatch shows the mismatch toast", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
    };
    mockSearchParamsValue = new URLSearchParams("error=refresh_user_mismatch");

    render(<ProfilePage />);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
    const errorToast = mockToast.mock.calls[0][0];
    expect(errorToast.variant).toBe("destructive");
    expect(errorToast.description).toBe(
      "refreshFromIdPErrorMismatch|signInMethodGoogle",
    );
    expect(mockRouterReplace).toHaveBeenCalledWith("/profile");
  });

  it("on ?error=refresh_state_expired shows the expired toast", async () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "github",
    };
    mockSearchParamsValue = new URLSearchParams("error=refresh_state_expired");

    render(<ProfilePage />);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
    const errorToast = mockToast.mock.calls[0][0];
    expect(errorToast.description).toBe(
      "refreshFromIdPErrorExpired|signInMethodGitHub",
    );
  });

  it("does nothing on a clean URL (no toast, no replace)", () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
    };
    mockSearchParamsValue = new URLSearchParams("");

    render(<ProfilePage />);

    expect(mockToast).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
  });
});

// ---------- Connected Accounts → Password section (#1678) --------------------

describe("ProfilePage — linked providers stay in sync (#1678)", () => {
  it("bumps the Password section's providers version on a link change", () => {
    mockUser = {
      id: "u1",
      email: "me@example.com",
      name: "Me",
      auth_method: "oauth",
      auth_provider: "github",
    };
    render(<ProfilePage />);
    expect(screen.getByTestId("password-settings-stub").textContent).toBe("0");

    fireEvent.click(screen.getByText("connected-accounts-stub"));

    expect(screen.getByTestId("password-settings-stub").textContent).toBe("1");
  });
});
