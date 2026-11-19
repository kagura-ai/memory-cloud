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

// /system/info never answers: the gate must not need it. An admin route
// (#2767) needs it, so a test may set the answer.
let mockSystemInfo: { features: Record<string, boolean> } | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () => mockSystemInfo,
  useSystemFeatures: () => mockSystemInfo?.features ?? null,
  isSystemInfoUnavailable: () => false,
}));

vi.mock("@/i18n", () => ({
  useLocale: () => ({ locale: "en", setLocale: vi.fn() }),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}));

let mockPathname = "/workspace/dashboard";
const mockPush = vi.fn();
const mockReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
  usePathname: () => mockPathname,
  useSearchParams: () => new URLSearchParams(),
}));

const mockSwitchWorkspace = vi.fn();
let mockHasSelection = true;
let mockWorkspaces: {
  id: string;
  name?: string;
  current_user_role?: string;
  slot_suspended?: boolean;
}[] = [];
// The app behind the gate — must not mount while the gate is up.
vi.mock("@/contexts/WorkspaceContext", () => ({
  WorkspaceProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app">{children}</div>
  ),
  useWorkspace: () => ({
    workspaces: mockWorkspaces,
    currentWorkspace: mockHasSelection ? mockWorkspaces[0] ?? null : null,
    loading: false,
    switchWorkspace: mockSwitchWorkspace,
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
  mockSwitchWorkspace.mockReset().mockResolvedValue(undefined);
  mockHasSelection = true;
  mockAcceptTerms.mockReset();
  mockPathname = "/workspace/dashboard";
  mockSystemInfo = null;
  mockWorkspaces = [{ id: "w1", current_user_role: "member" }];
  mockPush.mockClear();
  mockReplace.mockClear();
  mockUser = null;
  window.sessionStorage.clear();
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

  it("confirms a sole membership automatically before rendering the page", async () => {
    mockUser = { id: "u1", current_workspace_id: null };
    mockHasSelection = false;
    render(<DashboardLayout><p>protected page</p></DashboardLayout>);
    await waitFor(() => expect(mockSwitchWorkspace).toHaveBeenCalledWith("w1"));
    expect(screen.queryByText("selectWorkspace")).toBeNull();
    expect(screen.queryByText("protected page")).toBeNull();
  });

  it("allows a system admin to open platform users without any workspace", async () => {
    mockUser = { id: "u1", role: "admin" };
    mockPathname = "/admin/users";
    mockSystemInfo = { features: { system_admin: true } };
    mockWorkspaces = [];
    render(<DashboardLayout><p>platform users</p></DashboardLayout>);
    expect(await screen.findByText("platform users")).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
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

describe("(authenticated) layout — viewer landing (#2630)", () => {
  const signedIn = { id: "u1", email: "u@example.test", name: "U", terms_acceptance_required: false };

  it("sends a viewer who signs in to contexts, and explains the dashboard on a later menu click", async () => {
    mockUser = signedIn;
    mockWorkspaces = [{ id: "w1", current_user_role: "viewer" }];
    const { rerender } = render(<DashboardLayout><p>dashboard stats</p></DashboardLayout>);
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/workspace/contexts"));
    expect(screen.queryByText("dashboard stats")).toBeNull();

    mockPathname = "/workspace/contexts";
    rerender(<DashboardLayout><p>contexts list</p></DashboardLayout>);
    expect(await screen.findByText("contexts list")).toBeInTheDocument();

    // The sidebar's Dashboard link is a navigation inside the app, not a landing.
    mockReplace.mockClear();
    mockPathname = "/workspace/dashboard";
    rerender(<DashboardLayout><p>dashboard stats</p></DashboardLayout>);
    expect(await screen.findByText("member")).toBeInTheDocument();
    expect(screen.queryByText("dashboard stats")).toBeNull();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("keeps a member's sign-in landing on the dashboard", async () => {
    mockUser = signedIn;
    render(<DashboardLayout><p>dashboard stats</p></DashboardLayout>);
    expect(await screen.findByText("dashboard stats")).toBeInTheDocument();
    expect(mockReplace).not.toHaveBeenCalled();
  });
});

describe("(authenticated) layout — Workspace management (#2919)", () => {
  const owner = {
    id: "u1",
    email: "u@example.test",
    name: "U",
    terms_acceptance_required: false,
    current_workspace_id: "w1",
  };
  const twoOwned = [
    { id: "w1", name: "Personal", current_user_role: "owner", slot_suspended: true },
    { id: "w2", name: "Research", current_user_role: "owner" },
  ];

  it("exempts exactly /profile/workspaces from the selection screen", async () => {
    mockUser = owner;
    mockHasSelection = false;
    mockWorkspaces = twoOwned;
    mockPathname = "/profile/workspaces";
    const { rerender } = render(<DashboardLayout><p>management page</p></DashboardLayout>);
    expect(await screen.findByText("management page")).toBeInTheDocument();
    expect(screen.queryByText("selectWorkspace")).toBeNull();

    // /profile itself is not exempt.
    mockPathname = "/profile";
    rerender(<DashboardLayout><p>account settings</p></DashboardLayout>);
    expect(await screen.findByText("selectWorkspace")).toBeInTheDocument();
    expect(screen.queryByText("account settings")).toBeNull();
  });

  it("keeps the zero-workspace redirect on the management page", async () => {
    mockUser = owner;
    mockWorkspaces = [];
    mockPathname = "/profile/workspaces";
    render(<DashboardLayout><p>management page</p></DashboardLayout>);
    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/general?create=true"),
    );
  });

  it("auto-select skips a suspended sole membership and shows it disabled", async () => {
    mockUser = { ...owner, current_workspace_id: null };
    mockHasSelection = false;
    mockWorkspaces = [{ id: "w9", name: "Club", current_user_role: "member", slot_suspended: true }];
    render(<DashboardLayout><p>protected page</p></DashboardLayout>);
    expect(await screen.findByText("selectWorkspace")).toBeInTheDocument();
    expect(mockSwitchWorkspace).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Club/ })).toBeDisabled();
    expect(screen.getByText("card.askOwner")).toBeInTheDocument();
  });

  it("the selection screen links an owner's suspended workspace to the page and keeps the others selectable", async () => {
    mockUser = owner;
    mockHasSelection = false;
    mockWorkspaces = twoOwned;
    render(<DashboardLayout><p>protected page</p></DashboardLayout>);
    expect(await screen.findByText("selectWorkspace")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Personal/ })).toBeDisabled();
    expect(screen.getByRole("link", { name: "card.manage" })).toHaveAttribute(
      "href",
      "/profile/workspaces",
    );
    fireEvent.click(screen.getByRole("button", { name: /Personal/ }));
    expect(mockSwitchWorkspace).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Research/ }));
    await waitFor(() => expect(mockSwitchWorkspace).toHaveBeenCalledWith("w2"));
  });

  it("sends an owner who must choose to the page once per browser session", async () => {
    mockUser = { ...owner, workspace_selection_required: true };
    const first = render(<DashboardLayout><p>dashboard</p></DashboardLayout>);
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/profile/workspaces"));
    expect(mockPush).toHaveBeenCalledTimes(1);
    first.unmount();

    // A later sign-in landing (same browser session) is not redirected again.
    mockPush.mockClear();
    render(<DashboardLayout><p>dashboard</p></DashboardLayout>);
    expect(await screen.findByText("dashboard")).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("does not redirect from platform administration", async () => {
    mockUser = { ...owner, role: "admin", workspace_selection_required: true };
    mockSystemInfo = { features: { system_admin: true } };
    mockPathname = "/admin/users";
    render(<DashboardLayout><p>platform users</p></DashboardLayout>);
    expect(await screen.findByText("platform users")).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalledWith("/profile/workspaces");
  });

  it("does not redirect before the terms are accepted", async () => {
    mockUser = { ...owner, terms_acceptance_required: true, terms_version: "2026-09", workspace_selection_required: true };
    render(<DashboardLayout><p>dashboard</p></DashboardLayout>);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("on the page itself: no redirect, and it counts as this session's prompt", async () => {
    mockUser = { ...owner, workspace_selection_required: true };
    mockPathname = "/profile/workspaces";
    const first = render(<DashboardLayout><p>management page</p></DashboardLayout>);
    expect(await screen.findByText("management page")).toBeInTheDocument();
    first.unmount();
    mockPathname = "/workspace/dashboard";
    render(<DashboardLayout><p>dashboard</p></DashboardLayout>);
    expect(await screen.findByText("dashboard")).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("without sessionStorage it still redirects only once per page load", async () => {
    const getItem = vi
      .spyOn(Storage.prototype, "getItem")
      .mockImplementation(() => {
        throw new Error("blocked");
      });
    try {
      mockUser = { ...owner, workspace_selection_required: true };
      const first = render(<DashboardLayout><p>dashboard</p></DashboardLayout>);
      await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/profile/workspaces"));
      first.unmount();
      mockPush.mockClear();
      render(<DashboardLayout><p>dashboard</p></DashboardLayout>);
      expect(await screen.findByText("dashboard")).toBeInTheDocument();
      expect(mockPush).not.toHaveBeenCalled();
    } finally {
      getItem.mockRestore();
    }
  });
});

