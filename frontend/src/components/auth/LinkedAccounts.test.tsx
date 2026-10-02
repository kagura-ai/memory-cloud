/**
 * Tests for the LinkedAccounts section (Issue #1784 — link two accounts of
 * one person as one identity).
 *
 * Covers:
 *   - render: linked accounts get Unlink, linkable accounts get Link; a row
 *     falls back from name to email to id
 *   - Link → confirm dialog → POST identity-links, then the list AND the
 *     auth user refresh; cancel → no POST
 *   - Unlink → confirm dialog → POST identity-links/unlink, then both refresh
 *   - empty state when nothing is linked and nothing is linkable
 *   - #1803: a linkable row asks to sign in again unless both accounts
 *     signed in recently; 403 on link explains it inside the dialog
 *   - errors: 404 / 409 / other on link and 404 / other on unlink (in-dialog
 *     Alert, no toast), load failure (banner, no toast)
 *   - #1818: "Confirm with Google" starts a link-proof sign-in, offered only
 *     when a row needs a sign-in and Google is configured
 *   - i18n: every key the component reads exists in BOTH en.json and ja.json
 */

import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import LinkedAccounts from "./LinkedAccounts";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

// ---------- Mocks ------------------------------------------------------------

const stableTranslator = (key: string, values?: Record<string, unknown>) => {
  // Surface the key plus the interpolated account so tests can assert on both.
  if (values && "account" in values) return `${key}|${values.account}`;
  return key;
};
vi.mock("next-intl", () => ({
  useTranslations: (_namespace: string) => stableTranslator,
}));

const mockRefetchUser = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "me" }, refetchUser: mockRefetchUser }),
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

const { mockGetAuthConfig, mockClearState } = vi.hoisted(() => ({
  mockGetAuthConfig: vi.fn(),
  mockClearState: vi.fn(),
}));
vi.mock("@/lib/auth/auth", () => ({
  getAuthConfig: () => mockGetAuthConfig(),
}));
vi.mock("@/lib/auth/clearClientState", () => ({
  clearIdentityScopedClientState: () => mockClearState(),
}));

const LINKS = "/api/v1/me/account/identity-links";
const UNLINK = "/api/v1/me/account/identity-links/unlink";

const ADMIN = {
  user_id: "local:admin",
  email: "admin@example.com",
  name: "Admin",
  linked_at: "2026-01-01T00:00:00Z",
};
const GOOGLE = { user_id: "google:1", email: "me@example.com", name: "Me" };

beforeEach(() => {
  mockToast.mockClear();
  mockRefetchUser.mockReset();
  mockRefetchUser.mockResolvedValue(undefined);
  mockApiGet.mockReset();
  mockApiPost.mockReset();
  mockClearState.mockReset();
  mockGetAuthConfig.mockReset();
  mockGetAuthConfig.mockResolvedValue({
    password_login_enabled: true,
    google_oauth_enabled: true,
    github_oauth_enabled: true,
  });
});

// ---------- render ----------------------------------------------------------

describe("LinkedAccounts — render", () => {
  it("lists linked accounts with Unlink and linkable accounts with Link", async () => {
    mockApiGet.mockResolvedValueOnce({ linked: [ADMIN], linkable: [GOOGLE] });

    render(<LinkedAccounts />);

    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledWith(LINKS);
    });
    expect(
      await screen.findByRole("button", { name: /^unlinkButtonLabel\|Admin$/ }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /^linkButtonLabel\|Me$/ }),
    ).toBeTruthy();
    // Each account sits under its own group heading, with its email shown.
    expect(screen.getByText("linkedHeading")).toBeTruthy();
    expect(screen.getByText("linkableHeading")).toBeTruthy();
    expect(screen.getByText("admin@example.com")).toBeTruthy();
    expect(screen.getByText("me@example.com")).toBeTruthy();
    // No cross-over, and no empty state.
    expect(
      screen.queryByRole("button", { name: /^linkButtonLabel\|Admin$/ }),
    ).toBeNull();
    expect(screen.queryByText("emptyDescription")).toBeNull();
    // What linking does and does not do is always stated.
    expect(screen.getByText("description")).toBeTruthy();
  });

  it("labels a row by email, then by id, when the name is missing", async () => {
    mockApiGet.mockResolvedValueOnce({
      linked: [{ user_id: "u-email", email: "only@example.com", name: null }],
      linkable: [{ user_id: "u-bare", email: null, name: null }],
    });

    render(<LinkedAccounts />);

    expect(
      await screen.findByRole("button", {
        name: /^unlinkButtonLabel\|only@example\.com$/,
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /^linkButtonLabel\|u-bare$/ }),
    ).toBeTruthy();
    expect(screen.getByText("u-bare")).toBeTruthy();
  });

  it("explains how to get a linkable account when there is nothing to show", async () => {
    mockApiGet.mockResolvedValueOnce({ linked: [], linkable: [] });

    render(<LinkedAccounts />);

    expect(await screen.findByText("emptyDescription")).toBeTruthy();
    expect(screen.queryByText("linkedHeading")).toBeNull();
    expect(screen.queryByText("linkableHeading")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows a banner, not a toast or the empty state, when the list fails to load", async () => {
    mockApiGet.mockRejectedValueOnce(new FakeApiError(500));

    render(<LinkedAccounts />);

    expect(await screen.findByText("loadError")).toBeTruthy();
    expect(screen.queryByText("emptyDescription")).toBeNull();
    expect(mockToast).not.toHaveBeenCalled();
  });
});

// ---------- link -------------------------------------------------------------

describe("LinkedAccounts — link", () => {
  async function openLinkDialog() {
    fireEvent.click(
      await screen.findByRole("button", { name: /^linkButtonLabel\|Me$/ }),
    );
    return screen.findByRole("alertdialog");
  }
  async function confirmLink() {
    const dialog = await openLinkDialog();
    fireEvent.click(
      within(dialog).getByRole("button", { name: /^linkButton$/ }),
    );
  }

  it("asks for confirmation before POSTing, and cancel sends nothing", async () => {
    mockApiGet.mockResolvedValueOnce({ linked: [], linkable: [GOOGLE] });

    render(<LinkedAccounts />);
    const dialog = await openLinkDialog();

    expect(within(dialog).getByText("linkTitle")).toBeTruthy();
    expect(within(dialog).getByText("linkDescription|Me")).toBeTruthy();
    expect(mockApiPost).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(mockApiPost).not.toHaveBeenCalled();
    expect(mockRefetchUser).not.toHaveBeenCalled();
  });

  it("on confirm POSTs the account id, then refreshes the list and the auth user", async () => {
    mockApiGet
      .mockResolvedValueOnce({ linked: [], linkable: [GOOGLE] })
      .mockResolvedValueOnce({ linked: [GOOGLE], linkable: [] });
    mockApiPost.mockResolvedValueOnce({ status: "ok" });

    render(<LinkedAccounts />);
    await confirmLink();

    await waitFor(() => {
      expect(mockApiPost).toHaveBeenCalledWith(LINKS, { user_id: "google:1" });
    });
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({ title: "linkSuccess|Me" });
    });
    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledTimes(2);
    });
    expect(mockRefetchUser).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    // The account moved to the linked group.
    expect(
      await screen.findByRole("button", { name: /^unlinkButtonLabel\|Me$/ }),
    ).toBeTruthy();
  });

  it("shows the not-signed-in error inside the open dialog on 404 and re-reads the list", async () => {
    mockApiGet
      .mockResolvedValueOnce({ linked: [], linkable: [GOOGLE] })
      .mockResolvedValueOnce({ linked: [], linkable: [] });
    mockApiPost.mockRejectedValueOnce(new FakeApiError(404));

    render(<LinkedAccounts />);
    await confirmLink();

    expect(await screen.findByText("linkNotSignedInError|Me")).toBeTruthy();
    expect(screen.getByRole("alertdialog")).toBeTruthy();
    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledTimes(2);
    });
    expect(mockToast).not.toHaveBeenCalled();
    expect(mockRefetchUser).not.toHaveBeenCalled();
  });

  it("shows the sign-in-again error inside the open dialog on 403 and re-reads the list", async () => {
    mockApiGet
      .mockResolvedValueOnce({ linked: [], linkable: [GOOGLE] })
      .mockResolvedValueOnce({ linked: [], linkable: [GOOGLE] });
    mockApiPost.mockRejectedValueOnce(new FakeApiError(403));

    render(<LinkedAccounts />);
    await confirmLink();

    expect(await screen.findByText("linkSignInAgainError|Me")).toBeTruthy();
    expect(screen.getByRole("alertdialog")).toBeTruthy();
    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledTimes(2);
    });
    expect(mockToast).not.toHaveBeenCalled();
    expect(mockRefetchUser).not.toHaveBeenCalled();
  });

  it("shows the limit error inside the open dialog on 409", async () => {
    mockApiGet.mockResolvedValueOnce({ linked: [], linkable: [GOOGLE] });
    mockApiPost.mockRejectedValueOnce(new FakeApiError(409));

    render(<LinkedAccounts />);
    await confirmLink();

    expect(await screen.findByText("linkLimitError")).toBeTruthy();
    expect(screen.getByRole("alertdialog")).toBeTruthy();
    expect(mockToast).not.toHaveBeenCalled();
    expect(mockApiGet).toHaveBeenCalledTimes(1);
    expect(mockRefetchUser).not.toHaveBeenCalled();
  });

  it("shows the generic error inside the open dialog on any other failure", async () => {
    mockApiGet.mockResolvedValueOnce({ linked: [], linkable: [GOOGLE] });
    mockApiPost.mockRejectedValueOnce(new FakeApiError(500));

    render(<LinkedAccounts />);
    await confirmLink();

    expect(await screen.findByText("linkError|Me")).toBeTruthy();
    expect(mockToast).not.toHaveBeenCalled();
    expect(mockRefetchUser).not.toHaveBeenCalled();
  });
});

// ---------- #1803: a recent sign-in of both accounts -------------------------

describe("LinkedAccounts — recent sign-in", () => {
  it("asks to sign in again when the linkable account's sign-in is old", async () => {
    mockApiGet.mockResolvedValueOnce({
      linked: [],
      linkable: [{ ...GOOGLE, signed_in_recently: false }],
      signed_in_recently: true,
      sign_in_window_minutes: 10,
    });

    render(<LinkedAccounts />);

    expect(await screen.findByText("signInAgainHint")).toBeTruthy();
  });

  it("asks to sign in again when this account's own sign-in is old", async () => {
    mockApiGet.mockResolvedValueOnce({
      linked: [],
      linkable: [{ ...GOOGLE, signed_in_recently: true }],
      signed_in_recently: false,
      sign_in_window_minutes: 10,
    });

    render(<LinkedAccounts />);

    expect(await screen.findByText("signInAgainHint")).toBeTruthy();
  });

  it("says nothing when both signed in recently, and never on linked rows", async () => {
    mockApiGet.mockResolvedValueOnce({
      linked: [ADMIN],
      linkable: [{ ...GOOGLE, signed_in_recently: true }],
      signed_in_recently: true,
      sign_in_window_minutes: 10,
    });

    render(<LinkedAccounts />);

    expect(await screen.findByText("Me")).toBeTruthy();
    expect(screen.queryByText("signInAgainHint")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "confirmWithGoogle" }),
    ).toBeNull();
  });
});

// ---------- #1818: a proved sign-in ------------------------------------------

describe("LinkedAccounts — confirm with Google", () => {
  const stale = {
    linked: [],
    linkable: [{ ...GOOGLE, signed_in_recently: false }],
    signed_in_recently: true,
    sign_in_window_minutes: 10,
  };

  it("starts a link-proof Google sign-in that adds to this session", async () => {
    mockApiGet.mockResolvedValueOnce(stale);
    const assign = vi.fn();
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...original,
        assign,
        pathname: "/profile",
        origin: original.origin,
      },
    });
    try {
      render(<LinkedAccounts />);
      fireEvent.click(
        await screen.findByRole("button", { name: "confirmWithGoogle" }),
      );

      expect(mockClearState).toHaveBeenCalledTimes(1);
      const url = new URL(assign.mock.calls[0][0]);
      expect(url.pathname).toBe("/api/v1/auth/google/login");
      expect(url.searchParams.get("add_account")).toBe("1");
      expect(url.searchParams.get("link_proof")).toBe("1");
      expect(new URL(url.searchParams.get("return_to")!).pathname).toBe(
        "/profile",
      );
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        value: original,
      });
    }
  });

  it("is not offered when Google sign-in is not configured", async () => {
    mockGetAuthConfig.mockResolvedValue({
      password_login_enabled: true,
      google_oauth_enabled: false,
      github_oauth_enabled: true,
    });
    mockApiGet.mockResolvedValueOnce(stale);

    render(<LinkedAccounts />);

    expect(await screen.findByText("signInAgainHint")).toBeTruthy();
    await waitFor(() => expect(mockGetAuthConfig).toHaveBeenCalled());
    expect(
      screen.queryByRole("button", { name: "confirmWithGoogle" }),
    ).toBeNull();
  });
});

// ---------- unlink -----------------------------------------------------------

describe("LinkedAccounts — unlink", () => {
  async function openUnlinkDialog() {
    fireEvent.click(
      await screen.findByRole("button", { name: /^unlinkButtonLabel\|Admin$/ }),
    );
    return screen.findByRole("alertdialog");
  }

  it("asks for confirmation before POSTing", async () => {
    mockApiGet.mockResolvedValueOnce({ linked: [ADMIN], linkable: [] });

    render(<LinkedAccounts />);
    const dialog = await openUnlinkDialog();

    expect(within(dialog).getByText("unlinkTitle|Admin")).toBeTruthy();
    expect(mockApiPost).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it("on confirm POSTs unlink, then refreshes the list and the auth user", async () => {
    mockApiGet
      .mockResolvedValueOnce({ linked: [ADMIN], linkable: [] })
      .mockResolvedValueOnce({ linked: [], linkable: [ADMIN] });
    mockApiPost.mockResolvedValueOnce({ status: "ok" });

    render(<LinkedAccounts />);
    const dialog = await openUnlinkDialog();
    fireEvent.click(
      within(dialog).getByRole("button", { name: /^unlinkButton$/ }),
    );

    await waitFor(() => {
      expect(mockApiPost).toHaveBeenCalledWith(UNLINK, {
        user_id: "local:admin",
      });
    });
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({ title: "unlinkSuccess|Admin" });
    });
    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledTimes(2);
    });
    expect(mockRefetchUser).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("shows the not-linked error inside the open dialog on 404", async () => {
    mockApiGet.mockResolvedValueOnce({ linked: [ADMIN], linkable: [] });
    mockApiPost.mockRejectedValueOnce(new FakeApiError(404));

    render(<LinkedAccounts />);
    const dialog = await openUnlinkDialog();
    fireEvent.click(
      within(dialog).getByRole("button", { name: /^unlinkButton$/ }),
    );

    expect(await screen.findByText("unlinkNotLinkedError|Admin")).toBeTruthy();
    expect(screen.getByRole("alertdialog")).toBeTruthy();
    expect(mockToast).not.toHaveBeenCalled();
    expect(mockRefetchUser).not.toHaveBeenCalled();
  });

  it("shows the generic error inside the open dialog on any other failure", async () => {
    mockApiGet.mockResolvedValueOnce({ linked: [ADMIN], linkable: [] });
    mockApiPost.mockRejectedValueOnce(new FakeApiError(500));

    render(<LinkedAccounts />);
    const dialog = await openUnlinkDialog();
    fireEvent.click(
      within(dialog).getByRole("button", { name: /^unlinkButton$/ }),
    );

    expect(await screen.findByText("unlinkError|Admin")).toBeTruthy();
    expect(mockToast).not.toHaveBeenCalled();
  });
});

// ---------- i18n: keys exist in both locales (no hardcoded strings) ----------

describe("LinkedAccounts — i18n key coverage", () => {
  const NS = "linkedAccounts";
  const REQUIRED_KEYS = [
    "title",
    "description",
    "linkedHeading",
    "linkableHeading",
    "linkButton",
    "linkButtonLabel",
    "linking",
    "linkTitle",
    "linkDescription",
    "unlinkButton",
    "unlinkButtonLabel",
    "unlinking",
    "unlinkTitle",
    "unlinkDescription",
    "emptyTitle",
    "emptyDescription",
    "linkSuccess",
    "unlinkSuccess",
    "linkError",
    "linkNotSignedInError",
    "linkSignInAgainError",
    "signInAgainHint",
    "linkLimitError",
    "unlinkError",
    "unlinkNotLinkedError",
    "loadError",
  ];

  it.each([
    ["en", en],
    ["ja", ja],
  ] as const)("defines every linkedAccounts key in %s.json", (_locale, cat) => {
    const section = (cat as unknown as Record<string, Record<string, string>>)[
      NS
    ];
    expect(section).toBeTruthy();
    for (const key of REQUIRED_KEYS) {
      expect(section[key], `missing linkedAccounts.${key}`).toBeTruthy();
    }
  });

  it("titles the section exactly 'Linked accounts' in English", () => {
    expect(en.linkedAccounts.title).toBe("Linked accounts");
  });
});
