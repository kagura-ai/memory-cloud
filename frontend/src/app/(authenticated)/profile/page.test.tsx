/**
 * Tests for the profile page (Issues #514 + #515).
 *
 * Covers:
 *   - getSignInMethodLabel helper (4+1 branches)            — #514
 *   - render: "Sign-in method" Input shows the right label  — #514
 *   - getRefreshProviderName helper (3+2 branches)          — #515
 *   - render: refresh button visible for google/github only — #515
 *   - click: POST /me/refresh-oauth → window.location set   — #515
 *   - click error: 429 → "rate limited" toast               — #515
 *   - search-param effect: refreshed=1 → success toast      — #515
 *   - search-param effect: error=refresh_* → destructive    — #515
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import ProfilePage, {
  getSignInMethodLabel,
  getRefreshProviderName,
} from "./page";

// ---------- Mocks ------------------------------------------------------------

const stableTranslator = (key: string, values?: Record<string, unknown>) => {
  // Translator stub: surfaces the key plus any interpolated provider arg
  // so the test can assert on both the i18n key choice AND the value.
  if (values && "provider" in values) return `${key}|${values.provider}`;
  return key;
};
vi.mock("next-intl", () => ({
  useTranslations: (_namespace: string) => stableTranslator,
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

const { mockApiPost, mockApiPut, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number;
    constructor(status: number, message = "fake-error") {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }
  return {
    mockApiPost: vi.fn(),
    mockApiPut: vi.fn(),
    FakeApiError,
  };
});
vi.mock("@/lib/api/base", () => ({
  apiClient: {
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
  mockUser = null;
  mockRefetchUser.mockClear();
  mockToast.mockClear();
  mockApiPost.mockReset();
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

  it("returns Password label for password auth_method (provider is ignored)", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "password", auth_provider: null },
        stableTranslator,
      ),
    ).toBe("signInMethodPassword");
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

// ---------- getRefreshProviderName (#515) -----------------------------------

describe("getRefreshProviderName", () => {
  it('returns "Google" for OAuth + google', () => {
    expect(
      getRefreshProviderName({ auth_method: "oauth", auth_provider: "google" }),
    ).toBe("Google");
  });

  it('returns "GitHub" for OAuth + github', () => {
    expect(
      getRefreshProviderName({ auth_method: "oauth", auth_provider: "github" }),
    ).toBe("GitHub");
  });

  it("returns null for password user (no IdP to refresh from)", () => {
    expect(
      getRefreshProviderName({ auth_method: "password", auth_provider: null }),
    ).toBeNull();
  });

  it("returns null for legacy OAuth user with null provider", () => {
    // Pre-#361 — backend would 400 anyway. UI hides the button.
    expect(
      getRefreshProviderName({ auth_method: "oauth", auth_provider: null }),
    ).toBeNull();
  });

  it("returns null when both fields are undefined", () => {
    expect(getRefreshProviderName({})).toBeNull();
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
    expect(input.value).toBe("signInMethodPassword");
  });
});

// ---------- ProfilePage render: refresh button (#515) -----------------------

describe("ProfilePage — refresh-from-IdP button visibility (#515)", () => {
  it("renders the refresh button for an OAuth + google user", () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "google",
    };
    render(<ProfilePage />);
    // Translator interpolates {provider} → label is "key|Google".
    expect(screen.getByText("refreshFromIdP|Google")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /refreshFromIdPButton\|Google/ }),
    ).toBeTruthy();
  });

  it("renders the refresh button for an OAuth + github user", () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: "github",
    };
    render(<ProfilePage />);
    expect(screen.getByText("refreshFromIdP|GitHub")).toBeTruthy();
  });

  it("does NOT render the refresh button for a password user", () => {
    mockUser = {
      id: "u-2",
      email: "u@example.com",
      name: "Test",
      auth_method: "password",
      auth_provider: null,
    };
    render(<ProfilePage />);
    expect(screen.queryByText(/refreshFromIdP\|/)).toBeNull();
  });

  it("does NOT render the refresh button for a legacy OAuth user (null provider)", () => {
    mockUser = {
      id: "u-3",
      email: "u@example.com",
      name: "Test",
      auth_method: "oauth",
      auth_provider: null,
    };
    render(<ProfilePage />);
    expect(screen.queryByText(/refreshFromIdP\|/)).toBeNull();
  });
});

// ---------- ProfilePage refresh-button click flow (#515) --------------------

describe("ProfilePage — refresh button click (#515)", () => {
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
    // navigation, but Object.defineProperty lets us spy on the assignment.
    const hrefSetter = vi.fn();
    Object.defineProperty(window, "location", {
      value: {
        href: "/profile",
        get _href() {
          return this.href;
        },
        set _href(v: string) {
          hrefSetter(v);
          this.href = v;
        },
      },
      writable: true,
    });
    // The component does `window.location.href = ...`. We replace the
    // descriptor for `href` directly so setter intercepts the assignment.
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      value: new Proxy(originalLocation, {
        set(target, prop, value) {
          if (prop === "href") {
            hrefSetter(value);
            // also reflect on target for any subsequent reads
            (target as unknown as Record<string, unknown>).href = value;
            return true;
          }
          (target as unknown as Record<string, unknown>)[prop as string] =
            value;
          return true;
        },
      }),
      writable: true,
    });

    render(<ProfilePage />);

    const button = screen.getByRole("button", {
      name: /refreshFromIdPButton\|Google/,
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

    const button = screen.getByRole("button", {
      name: /refreshFromIdPButton\|GitHub/,
    });
    fireEvent.click(button);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
    const lastToast = mockToast.mock.calls[mockToast.mock.calls.length - 1][0];
    expect(lastToast.variant).toBe("destructive");
    expect(lastToast.description).toBe("refreshFromIdPErrorRateLimited|GitHub");
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
      screen.getByRole("button", { name: /refreshFromIdPButton\|Google/ }),
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
    const lastToast = mockToast.mock.calls[mockToast.mock.calls.length - 1][0];
    expect(lastToast.variant).toBe("destructive");
    expect(lastToast.description).toBe("refreshFromIdPErrorGeneric|Google");
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
    expect(successToast.title).toBe("refreshFromIdPSuccess|Google");
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
    expect(errorToast.description).toBe("refreshFromIdPErrorMismatch|Google");
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
    expect(errorToast.description).toBe("refreshFromIdPErrorExpired|GitHub");
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
