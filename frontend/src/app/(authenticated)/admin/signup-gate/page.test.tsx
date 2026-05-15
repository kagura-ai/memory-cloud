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

function primeHappyPath(
  config = baseConfig,
  allowlist: Array<typeof sampleEntry> = [],
) {
  mockGetConfig.mockResolvedValue(config);
  mockListAllowlist.mockResolvedValue(allowlist);
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
    mockAddEntry.mockResolvedValue(sampleEntry);

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
    mockAddEntry.mockResolvedValue(googleEntry);

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
    mockRemoveEntry.mockResolvedValue(undefined);
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
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("renders an ErrorBanner on load failure", async () => {
    mockGetConfig.mockRejectedValue(new Error("network down"));
    mockListAllowlist.mockRejectedValue(new Error("network down"));

    render(<AdminSignupGatePage />);

    // ErrorBanner renders its message text
    await screen.findByText("network down");
  });
});
