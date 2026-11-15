/**
 * Tests for the Admin Signup Gate page (Issue #358 Phase 1).
 *
 * Covers:
 * - Initial render: page fetches config + allowlist on mount.
 * - Settings tab: toggling Enabled calls updateSignupGateConfig.
 * - Allowlist tab: submitting the form calls addSignupAllowlistEntry and
 *   updates the table; confirmation on delete.
 * - Error handling: load failure renders ErrorBanner; user-action failure
 *   surfaces a destructive toast.
 * - Paging (#2831): the list is read one page at a time from the server
 *   (`{items, total, limit, offset}`); add and remove re-read the page.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// ---------- Mocks ----------------------------------------------------------

const mockGetConfig = vi.fn();
const mockUpdateConfig = vi.fn();
const mockListAllowlist = vi.fn();
const mockAddEntry = vi.fn();
const mockRemoveEntry = vi.fn();
const mockToast = vi.fn();

vi.mock("@/lib/api/signup-gate", () => ({
  getSignupGateConfig: (...args: unknown[]) => mockGetConfig(...args),
  updateSignupGateConfig: (...args: unknown[]) => mockUpdateConfig(...args),
  listSignupAllowlist: (...args: unknown[]) => mockListAllowlist(...args),
  addSignupAllowlistEntry: (...args: unknown[]) => mockAddEntry(...args),
  removeSignupAllowlistEntry: (...args: unknown[]) => mockRemoveEntry(...args),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Namespace-aware translator so assertions can use fully-qualified keys
// (e.g. "admin.signupGate.title") regardless of which namespace the
// component opened with useTranslations.
vi.mock("next-intl", () => ({
  useTranslations:
    (namespace: string) =>
    (key: string, values?: Record<string, string | number>) => {
      const fullKey = namespace ? `${namespace}.${key}` : key;
      return values && Object.keys(values).length > 0
        ? `${fullKey}:${JSON.stringify(values)}`
        : fullKey;
    },
  useLocale: () => "en",
}));

// Stub the useTabParam hook — real hook reads searchParams, which needs
// next/navigation plumbing we don't need here. useState-backed so tab
// switching triggers a re-render.
//
// Tests that care about the allowlist tab can set `initialTab` to
// "allowlist" before rendering so the page mounts directly on that tab
// without needing a pointer-event click on Radix TabsTrigger (fireEvent.click
// alone does not satisfy the pointerdown/pointerup dance Radix Tabs listens
// for in jsdom).
let initialTab: "settings" | "allowlist" = "settings";
vi.mock("@/hooks/useTabParam", async () => {
  const react = await import("react");
  return {
    useTabParam: () => react.useState<string>(initialTab),
  };
});

// Minimize layout noise.
vi.mock("@/components/common/PageContainer", () => ({
  PageContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/common/PageHeader", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));

// Radix Select renders a visible combobox button + a hidden native
// <select> for form submission. The combobox uses pointer events that
// jsdom does not fire reliably, so we substitute a plain <select> that
// preserves the controlled-value contract (value / onValueChange) used
// by the page. This unblocks the provider-switch path coverage below.
type SelectChildren = { children?: React.ReactNode };
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
    disabled,
  }: {
    value: string;
    onValueChange?: (v: string) => void;
    children: React.ReactNode;
    disabled?: boolean;
  }) => (
    <select
      data-testid="select-mock"
      value={value}
      disabled={disabled}
      onChange={(e) => onValueChange?.(e.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: SelectChildren) => <>{children}</>,
  SelectValue: () => null,
  SelectContent: ({ children }: SelectChildren) => <>{children}</>,
  SelectItem: ({
    value,
    children,
  }: {
    value: string;
    children: React.ReactNode;
  }) => <option value={value}>{children}</option>,
}));

import AdminSignupGatePage from "./page";

// ---------- Helpers --------------------------------------------------------

const baseConfig = {
  enabled: false,
  mode: "manual" as const,
  github_sponsors_grace_period_days: 30,
};

const sampleEntry = {
  id: "entry-1",
  github_user_id: "583231",
  github_username: "octocat",
  source: "manual" as const,
  state: "active" as const,
  added_by_user_id: "admin1",
};

const PAGE_SIZE = 50;

/** The list route's answer (#2831) for one page of `items`. */
function page(
  items: Array<Record<string, unknown>>,
  total: number = items.length,
  offset: number = 0,
) {
  return { items, total, limit: PAGE_SIZE, offset };
}

function entries(count: number, start: number = 0) {
  return Array.from({ length: count }, (_, index) => ({
    ...sampleEntry,
    id: `entry-${start + index}`,
    github_username: `user-${start + index}`,
  }));
}

function primeHappyPath(
  config = baseConfig,
  allowlist: Array<typeof sampleEntry> = [],
) {
  mockGetConfig.mockResolvedValue(config);
  mockListAllowlist.mockResolvedValue(page(allowlist));
}

// ---------- Tests ----------------------------------------------------------

describe("AdminSignupGatePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    initialTab = "settings";
  });

  it("renders the page and fetches config + allowlist on mount", async () => {
    primeHappyPath();
    render(<AdminSignupGatePage />);

    await waitFor(() => {
      expect(mockGetConfig).toHaveBeenCalledTimes(1);
      expect(mockListAllowlist).toHaveBeenCalledTimes(1);
    });
    expect(mockListAllowlist).toHaveBeenCalledWith({
      limit: PAGE_SIZE,
      offset: 0,
    });

    expect(screen.getByText("admin.signupGate.title")).toBeInTheDocument();
  });

  it("renders the Enabled switch and Mode select after load", async () => {
    primeHappyPath();
    render(<AdminSignupGatePage />);
    const toggle = await screen.findByRole("switch");
    expect(toggle).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  it("saves config when the Enabled switch is toggled", async () => {
    primeHappyPath();
    mockUpdateConfig.mockResolvedValue({ ...baseConfig, enabled: true });

    render(<AdminSignupGatePage />);
    const toggle = await screen.findByRole("switch");
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(mockUpdateConfig).toHaveBeenCalledWith({
        enabled: true,
        mode: "manual",
      });
    });
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "admin.common.success" }),
    );
  });

  it("adds a user to the allowlist via the form", async () => {
    initialTab = "allowlist";
    primeHappyPath();
    // The list is re-read from the server after the add.
    mockAddEntry.mockImplementation(async () => {
      mockListAllowlist.mockResolvedValue(page([sampleEntry]));
      return sampleEntry;
    });

    render(<AdminSignupGatePage />);

    const input = await screen.findByPlaceholderText(
      "admin.signupGate.addUsername",
    );
    fireEvent.change(input, { target: { value: "octocat" } });

    const addButton = screen.getByRole("button", {
      name: /admin\.signupGate\.addButton/,
    });
    fireEvent.click(addButton);

    await waitFor(() => {
      // Issue #655: payload is now a discriminated union {provider, ...}
      // instead of a bare username string. Default provider is GitHub
      // so the form submits `{provider: "github", github_username}`.
      expect(mockAddEntry).toHaveBeenCalledWith({
        provider: "github",
        github_username: "octocat",
      });
    });
    await screen.findByText(/octocat/);
    expect(mockListAllowlist).toHaveBeenCalledTimes(2);
    expect(mockListAllowlist).toHaveBeenLastCalledWith({
      limit: PAGE_SIZE,
      offset: 0,
    });
  });

  it("adds a Google entry when the provider is switched to Google", async () => {
    // Issue #670 acceptance: switching the provider Select to "google"
    // routes the submit through the Google branch with the canonical
    // `{provider: "google", email}` payload (matching the backend's
    // AllowlistAddRequest discriminated union). Without this test the
    // Google submit path would regress silently on future refactors.
    initialTab = "allowlist";
    primeHappyPath();
    const googleEntry = {
      ...sampleEntry,
      provider: "google" as const,
      subject_id: "google-sub-1",
      subject_label: "alice@example.com",
    };
    mockAddEntry.mockImplementation(async () => {
      mockListAllowlist.mockResolvedValue(page([googleEntry]));
      return googleEntry;
    });

    render(<AdminSignupGatePage />);

    // Wait for the form to render (GitHub placeholder is the initial state).
    await screen.findByPlaceholderText("admin.signupGate.addUsername");

    // Provider selector is rendered via the mocked Select (plain <select>),
    // so a normal change event drives the controlled value.
    const providerSelect = screen.getByTestId("select-mock");
    fireEvent.change(providerSelect, { target: { value: "google" } });

    // Placeholder + aria-label switch to the email variant.
    const emailInput = await screen.findByPlaceholderText(
      "admin.signupGate.addEmail",
    );
    fireEvent.change(emailInput, { target: { value: "alice@example.com" } });

    fireEvent.click(
      screen.getByRole("button", { name: /admin\.signupGate\.addButton/ }),
    );

    await waitFor(() => {
      expect(mockAddEntry).toHaveBeenCalledWith({
        provider: "google",
        email: "alice@example.com",
      });
    });
    await screen.findByText(/alice@example.com/);
  });

  it("surfaces a destructive toast when add fails", async () => {
    initialTab = "allowlist";
    primeHappyPath();
    mockAddEntry.mockRejectedValue(new Error("boom"));

    render(<AdminSignupGatePage />);

    const input = await screen.findByPlaceholderText(
      "admin.signupGate.addUsername",
    );
    fireEvent.change(input, { target: { value: "ghost" } });
    fireEvent.click(
      screen.getByRole("button", { name: /admin\.signupGate\.addButton/ }),
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: "destructive" }),
      );
    });
  });

  it("removes an entry after confirmation", async () => {
    initialTab = "allowlist";
    primeHappyPath(baseConfig, [sampleEntry]);
    mockRemoveEntry.mockImplementation(async () => {
      mockListAllowlist.mockResolvedValue(page([]));
    });
    const confirmMock = vi.fn(() => true);
    vi.stubGlobal("confirm", confirmMock);

    try {
      render(<AdminSignupGatePage />);

      await screen.findByText(/octocat/);

      const deleteButton = screen.getByRole("button", {
        name: /admin\.signupGate\.removeAllowlistEntry/,
      });
      fireEvent.click(deleteButton);

      await waitFor(() => {
        expect(mockRemoveEntry).toHaveBeenCalledWith("entry-1");
      });
      expect(confirmMock).toHaveBeenCalled();
      // The page is re-read; the empty state replaces the table.
      await screen.findByText("admin.signupGate.allowlistEmpty");
      expect(screen.queryByText(/octocat/)).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("reads the next and the previous page from the server", async () => {
    initialTab = "allowlist";
    mockGetConfig.mockResolvedValue(baseConfig);
    mockListAllowlist.mockImplementation(
      async ({ offset }: { offset: number }) =>
        page(entries(offset === 100 ? 20 : PAGE_SIZE, offset), 120, offset),
    );

    render(<AdminSignupGatePage />);

    await screen.findByText("user-0");
    expect(
      screen.getByText(
        'admin.signupGate.pagination.showing:{"from":1,"to":50,"total":120}',
      ),
    ).toBeInTheDocument();
    const previous = screen.getByRole("button", {
      name: /admin\.signupGate\.pagination\.previous/,
    });
    const next = screen.getByRole("button", {
      name: /admin\.signupGate\.pagination\.next/,
    });
    expect(previous).toBeDisabled();

    fireEvent.click(next);
    await screen.findByText("user-50");
    expect(mockListAllowlist).toHaveBeenLastCalledWith({
      limit: PAGE_SIZE,
      offset: 50,
    });
    expect(screen.queryByText("user-0")).toBeNull();

    fireEvent.click(next);
    await screen.findByText("user-100");
    expect(
      screen.getByText(
        'admin.signupGate.pagination.showing:{"from":101,"to":120,"total":120}',
      ),
    ).toBeInTheDocument();
    expect(next).toBeDisabled();

    fireEvent.click(previous);
    await screen.findByText("user-50");
    expect(mockListAllowlist).toHaveBeenLastCalledWith({
      limit: PAGE_SIZE,
      offset: 50,
    });
  });

  it("shows no pager when the list is empty", async () => {
    initialTab = "allowlist";
    primeHappyPath();

    render(<AdminSignupGatePage />);

    await screen.findByText("admin.signupGate.allowlistEmpty");
    expect(
      screen.queryByRole("button", {
        name: /admin\.signupGate\.pagination\.next/,
      }),
    ).toBeNull();
  });

  it("steps back a page when the last row of a later page is removed", async () => {
    initialTab = "allowlist";
    mockGetConfig.mockResolvedValue(baseConfig);
    let total = 51;
    mockListAllowlist.mockImplementation(
      async ({ offset }: { offset: number }) =>
        page(
          entries(Math.max(0, Math.min(PAGE_SIZE, total - offset)), offset),
          total,
          offset,
        ),
    );
    mockRemoveEntry.mockImplementation(async () => {
      total = 50;
    });
    vi.stubGlobal(
      "confirm",
      vi.fn(() => true),
    );

    try {
      render(<AdminSignupGatePage />);
      await screen.findByText("user-0");
      fireEvent.click(
        screen.getByRole("button", {
          name: /admin\.signupGate\.pagination\.next/,
        }),
      );
      await screen.findByText("user-50");

      fireEvent.click(
        screen.getByRole("button", {
          name: /admin\.signupGate\.removeAllowlistEntry/,
        }),
      );

      await waitFor(() => {
        expect(mockRemoveEntry).toHaveBeenCalledWith("entry-50");
      });
      await screen.findByText("user-49");
      expect(mockListAllowlist).toHaveBeenLastCalledWith({
        limit: PAGE_SIZE,
        offset: 0,
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("renders an ErrorBanner, not a toast, when a page fails to load", async () => {
    initialTab = "allowlist";
    mockGetConfig.mockResolvedValue(baseConfig);
    mockListAllowlist.mockImplementation(
      async ({ offset }: { offset: number }) => {
        if (offset > 0) throw new Error("page two is down");
        return page(entries(PAGE_SIZE), 120, 0);
      },
    );

    render(<AdminSignupGatePage />);
    await screen.findByText("user-0");
    fireEvent.click(
      screen.getByRole("button", {
        name: /admin\.signupGate\.pagination\.next/,
      }),
    );

    await screen.findByText("page two is down");
    expect(mockToast).not.toHaveBeenCalled();
    // The add form stays usable.
    expect(
      screen.getByPlaceholderText("admin.signupGate.addUsername"),
    ).toBeInTheDocument();
  });

  it("labels an email sign-up row (#1734) and its beta-invite source", async () => {
    initialTab = "allowlist";
    mockGetConfig.mockResolvedValue(baseConfig);
    mockListAllowlist.mockResolvedValue(
      page([
        {
          ...sampleEntry,
          id: "entry-email",
          provider: "email",
          subject_id: "email:00000000-0000-4000-8000-000000000000",
          subject_label: "new@example.com",
          github_user_id: "email:legacy",
          github_username: "new@example.com",
          source: "beta_invite",
        },
      ]),
    );

    render(<AdminSignupGatePage />);

    expect(await screen.findByText("new@example.com")).toBeInTheDocument();
    expect(
      screen.getByText("admin.signupGate.providerEmail"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("admin.signupGate.source.beta_invite"),
    ).toBeInTheDocument();
    // Not a GitHub row: no GitHub id suffix.
    expect(screen.queryByText(/admin\.signupGate\.githubUserId/)).toBeNull();
  });

  it("renders an ErrorBanner on load failure", async () => {
    mockGetConfig.mockRejectedValue(new Error("network down"));
    mockListAllowlist.mockRejectedValue(new Error("network down"));

    render(<AdminSignupGatePage />);

    // ErrorBanner renders its message text
    await screen.findByText("network down");
  });
});
