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
 *   - errors: 404 / 409 / other on link and 404 / other on unlink (in-dialog
 *     Alert, no toast), load failure (banner, no toast)
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
