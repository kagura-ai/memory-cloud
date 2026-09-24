/**
 * The (authenticated) layout's terms re-acceptance gate (#1665).
 *
 * Pinned: while /auth/me reports `terms_acceptance_required`, the dialog is
 * shown INSTEAD of the app, and its version comes from /auth/me itself — so
 * accepting works even when /system/info never answers.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

const mockRefetchUser = vi.fn();
const mockLogout = vi.fn();
let mockUser: Record<string, unknown> | null = null;
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: mockUser,
    isLoading: false,
    refetchUser: mockRefetchUser,
    logout: mockLogout,
  }),
}));

const mockAcceptTerms = vi.fn();
vi.mock("@/lib/auth/auth", () => ({
  acceptTerms: (...args: unknown[]) => mockAcceptTerms(...args),
}));

// /system/info never answers: the gate must not need it.
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () => null,
  useSystemFeatures: () => null,
}));

vi.mock("@/i18n", () => ({
  useLocale: () => ({ locale: "en", setLocale: vi.fn() }),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/workspace/dashboard",
  useSearchParams: () => new URLSearchParams(),
}));

// The app behind the gate — must not mount while the gate is up.
vi.mock("@/contexts/WorkspaceContext", () => ({
  WorkspaceProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app">{children}</div>
  ),
  useWorkspace: () => ({
    workspaces: [{ id: "w1" }],
    currentWorkspace: { id: "w1" },
    loading: false,
    switchWorkspace: vi.fn(),
  }),
}));
vi.mock("@/contexts/MemoryContextContext", () => ({
  MemoryContextProvider: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
}));
vi.mock("@/components/dashboard/Sidebar", () => ({ Sidebar: () => null }));

import DashboardLayout from "./layout";

beforeEach(() => {
  mockRefetchUser.mockReset();
  mockLogout.mockReset();
  mockAcceptTerms.mockReset();
  mockUser = null;
});

afterEach(() => {
  cleanup();
});

describe("(authenticated) layout — terms re-acceptance gate (#1665)", () => {
  it("shows the dialog instead of the app and accepts the /auth/me version", async () => {
    mockUser = {
      id: "u1",
      email: "u@example.test",
      name: "U",
      terms_acceptance_required: true,
      terms_version: "2026-09",
    };
    mockAcceptTerms.mockResolvedValue({
      version: "2026-09",
      recorded: true,
      terms_acceptance_required: false,
    });
    mockRefetchUser.mockResolvedValue(undefined);

    render(
      <DashboardLayout>
        <p>page</p>
      </DashboardLayout>,
    );

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByTestId("app")).toBeNull();
    expect(screen.queryByText("page")).toBeNull();

    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/i }));
    const accept = screen.getByRole("button", { name: "accept" });
    expect(accept).not.toBeDisabled();
    fireEvent.click(accept);

    await waitFor(() => expect(mockRefetchUser).toHaveBeenCalledTimes(1));
    expect(mockAcceptTerms).toHaveBeenCalledWith("2026-09");
  });

  it("renders the app when no re-acceptance is required", async () => {
    mockUser = {
      id: "u1",
      email: "u@example.test",
      name: "U",
      terms_acceptance_required: false,
      terms_version: null,
    };

    render(
      <DashboardLayout>
        <p>page</p>
      </DashboardLayout>,
    );

    expect(await screen.findByText("page")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
