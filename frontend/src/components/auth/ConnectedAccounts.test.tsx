/**
 * Tests for the ConnectedAccounts section (Issue #517 — multi-provider OAuth
 * account linking, Task 7 frontend).
 *
 * Covers:
 *   - render: a linked provider (google) shows a Disconnect affordance and an
 *     unlinked provider (github) shows a Connect affordance
 *   - Connect → POST /me/account/link-provider then full-page navigation via
 *     window.location.href (NOT an in-page apiClient.get fetch)
 *   - i18n: every user-facing key the component reads exists in BOTH en.json
 *     and ja.json (no hardcoded strings)
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import ConnectedAccounts from "./ConnectedAccounts";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

// ---------- Mocks ------------------------------------------------------------

const stableTranslator = (key: string, values?: Record<string, unknown>) => {
  // Surface the key plus any interpolated provider arg so tests can assert on
  // both the i18n key choice AND the value, matching the profile page test
  // fixture convention (page.test.tsx).
  if (values && "provider" in values) return `${key}|${values.provider}`;
  return key;
};
vi.mock("next-intl", () => ({
  useTranslations: (_namespace: string) => stableTranslator,
}));

let mockUser: {
  auth_method?: "password" | "oauth";
  auth_provider?: "google" | "github" | null;
} | null = { auth_method: "password", auth_provider: null };
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: mockUser }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const { mockApiGet, mockApiPost, FakeApiError } = vi.hoisted(() => {
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
    FakeApiError,
  };
});
vi.mock("@/lib/api/base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockApiGet(...args),
    post: (...args: unknown[]) => mockApiPost(...args),
  },
  ApiError: FakeApiError,
}));

beforeEach(() => {
  mockUser = { auth_method: "password", auth_provider: null };
  mockToast.mockClear();
  mockApiGet.mockReset();
  mockApiPost.mockReset();
});

// ---------- render: linked vs unlinked affordances ---------------------------

describe("ConnectedAccounts — render", () => {
  it("shows Disconnect for a linked provider (google) and Connect for an unlinked one (github)", async () => {
    mockApiGet.mockResolvedValueOnce({
      providers: [
        {
          provider: "google",
          linked_at: "2026-01-01T00:00:00Z",
          last_used_at: "2026-02-01T00:00:00Z",
        },
      ],
    });

    render(<ConnectedAccounts />);

    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledWith("/api/v1/me/account/providers");
    });

    // Google is linked → a Disconnect affordance is present.
    // Anchor the names: "disconnectButton|google" contains the substring
    // "connectButton|google", so an unanchored regex would cross-match.
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /^disconnectButton\|google$/ }),
      ).toBeTruthy();
    });
    // GitHub is not linked → a Connect affordance is present.
    expect(
      screen.getByRole("button", { name: /^connectButton\|github$/ }),
    ).toBeTruthy();
    // GitHub has NO Disconnect, Google has NO Connect.
    expect(
      screen.queryByRole("button", { name: /^connectButton\|google$/ }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /^disconnectButton\|github$/ }),
    ).toBeNull();
  });
});

// ---------- Connect → window.location navigation -----------------------------

describe("ConnectedAccounts — connect navigation", () => {
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

  it("on Connect POSTs link-provider then sets window.location.href to authorization_url", async () => {
    mockApiGet.mockResolvedValueOnce({ providers: [] }); // nothing linked yet
    mockApiPost.mockResolvedValueOnce({
      authorization_url: "https://github.com/login/oauth/authorize?x=1",
      state: "st",
    });

    // Plain stub object so no real navigation is attempted (a real assignment
    // would trigger a swallowed happy-dom TypeError). We only need to observe
    // the href the component sets.
    Object.defineProperty(window, "location", {
      value: { href: "" },
      writable: true,
      configurable: true,
    });

    render(<ConnectedAccounts />);

    const connectBtn = await screen.findByRole("button", {
      name: /^connectButton\|github$/,
    });
    fireEvent.click(connectBtn);

    await waitFor(() => {
      expect(mockApiPost).toHaveBeenCalledWith(
        "/api/v1/me/account/link-provider",
        { provider: "github" },
      );
    });
    // Full-page navigation — NOT an apiClient.get consumed in-page.
    await waitFor(() => {
      expect(window.location.href).toBe(
        "https://github.com/login/oauth/authorize?x=1",
      );
    });
  });
});

// ---------- disconnect: 409 / last-method / success paths --------------------

describe("ConnectedAccounts — disconnect", () => {
  it("surfaces an in-dialog error and keeps the dialog open when unlink returns 409", async () => {
    // Password user with BOTH providers linked → Disconnect is allowed (the
    // 409 is a defensive server-side guard, not pre-empted by isOnlyMethod).
    mockApiGet.mockResolvedValueOnce({
      providers: [{ provider: "google" }, { provider: "github" }],
    });
    mockApiPost.mockRejectedValueOnce(new FakeApiError(409));

    render(<ConnectedAccounts />);

    const disconnectBtn = await screen.findByRole("button", {
      name: /^disconnectButton\|google$/,
    });
    fireEvent.click(disconnectBtn);

    // Confirm inside the dialog.
    const confirmBtn = await screen.findByRole("button", {
      name: /^disconnectConfirm$/,
    });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockApiPost).toHaveBeenCalledWith(
        "/api/v1/me/account/unlink-provider",
        { provider: "google" },
      );
    });

    // The in-dialog Alert shows the last-method error text.
    expect(await screen.findByText("lastMethodError")).toBeTruthy();
    // Dialog stays open → no success toast, and providers were NOT reloaded
    // (the initial mount fetch is the only GET).
    expect(mockToast).not.toHaveBeenCalled();
    expect(mockApiGet).toHaveBeenCalledTimes(1);
  });

  it("disables Disconnect (with a hint) when the provider is the only sign-in method", async () => {
    // OAuth-only user with a single linked provider must keep it.
    mockUser = { auth_method: "oauth", auth_provider: "google" };
    mockApiGet.mockResolvedValueOnce({
      providers: [{ provider: "google" }],
    });

    render(<ConnectedAccounts />);

    const disconnectBtn = await screen.findByRole("button", {
      name: /^disconnectButton\|google$/,
    });
    expect((disconnectBtn as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("lastMethodHint")).toBeTruthy();
  });

  it("toasts success and reloads providers on a successful disconnect", async () => {
    // Password user with both linked → unlink google succeeds.
    mockApiGet
      .mockResolvedValueOnce({
        providers: [{ provider: "google" }, { provider: "github" }],
      })
      .mockResolvedValueOnce({ providers: [{ provider: "github" }] });
    mockApiPost.mockResolvedValueOnce({ status: "ok" });

    render(<ConnectedAccounts />);

    const disconnectBtn = await screen.findByRole("button", {
      name: /^disconnectButton\|google$/,
    });
    fireEvent.click(disconnectBtn);

    const confirmBtn = await screen.findByRole("button", {
      name: /^disconnectConfirm$/,
    });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: "disconnectSuccess|google",
      });
    });
    // Providers reloaded: initial mount GET + post-success reload GET.
    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledTimes(2);
    });
  });
});

// ---------- i18n: keys exist in both locales (no hardcoded strings) ----------

describe("ConnectedAccounts — i18n key coverage", () => {
  const NS = "connectedAccounts";
  const REQUIRED_KEYS = [
    "title",
    "description",
    "google",
    "github",
    "connected",
    "notConnected",
    "connectButton",
    "disconnectButton",
    "disconnectConfirm",
    "connecting",
    "disconnecting",
    "disconnectTitle",
    "disconnectDescription",
    "disconnectSuccess",
    "connectError",
    "disconnectError",
    "loadError",
    "lastMethodError",
    "lastMethodHint",
  ];

  it("defines the connectedAccounts namespace in en.json with all required keys", () => {
    const section = (en as unknown as Record<string, Record<string, string>>)[
      NS
    ];
    expect(section).toBeTruthy();
    for (const key of REQUIRED_KEYS) {
      expect(
        section[key],
        `en.json missing connectedAccounts.${key}`,
      ).toBeTruthy();
    }
  });

  it("defines the connectedAccounts namespace in ja.json with all required keys", () => {
    const section = (ja as unknown as Record<string, Record<string, string>>)[
      NS
    ];
    expect(section).toBeTruthy();
    for (const key of REQUIRED_KEYS) {
      expect(
        section[key],
        `ja.json missing connectedAccounts.${key}`,
      ).toBeTruthy();
    }
  });
});
