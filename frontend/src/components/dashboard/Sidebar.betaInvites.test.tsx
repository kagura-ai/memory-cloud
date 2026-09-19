/**
 * Sidebar × beta invites (#1582): the "Invite a friend" card above the account
 * menu, and the permanent account-menu entry next to Profile Settings.
 *
 * A separate file from Sidebar.test.tsx because it swaps the Radix dropdown
 * for a pass-through: this repo has no working pattern for opening one under a
 * DOM emulator, and the entry under test lives inside it.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockGetMine = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/beta-invites", () => ({
  getMyBetaInvites: (...a: unknown[]) => mockGetMine(...a),
  createBetaInvite: vi.fn(),
  revokeBetaInvite: vi.fn(),
}));

vi.mock("@/components/ui/dropdown-menu", () => {
  const Pass = ({ children }: { children?: React.ReactNode }) => (
    <>{children}</>
  );
  return {
    DropdownMenu: Pass,
    DropdownMenuTrigger: Pass,
    DropdownMenuContent: ({ children }: { children?: React.ReactNode }) => (
      <div role="menu">{children}</div>
    ),
    DropdownMenuItem: ({
      children,
      onClick,
    }: {
      children?: React.ReactNode;
      onClick?: () => void;
    }) => (
      <div role="menuitem" onClick={onClick}>
        {children}
      </div>
    ),
    DropdownMenuLabel: Pass,
    DropdownMenuSeparator: () => <hr />,
    DropdownMenuSub: Pass,
    DropdownMenuSubTrigger: Pass,
    DropdownMenuSubContent: Pass,
  };
});

vi.mock("next-intl", () => ({
  useLocale: () => "en",
  useTranslations:
    (_ns: string) => (key: string, vars?: Record<string, unknown>) =>
      vars && Object.keys(vars).length > 0
        ? `${key}:${JSON.stringify(vars)}`
        : key,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/workspace/dashboard",
  useSearchParams: () => new URLSearchParams(),
}));

const mockUser = {
  id: "u1",
  name: "Test User",
  email: "test@example.com",
  picture: "",
  role: "user" as "user" | "admin",
};
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: mockUser, logout: vi.fn(), isAuthenticated: true }),
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: {
      id: "w1",
      name: "Test WS",
      current_user_role: "member",
      member_count: 1,
    },
    currentWorkspaceId: "w1",
  }),
}));

vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanFeatures: () => ({
    resources: true,
    connectors: true,
    public_contexts: true,
  }),
}));

vi.mock("@/lib/api/contexts", () => ({
  getContexts: vi.fn().mockResolvedValue({ contexts: [{ id: "c1" }] }),
}));

vi.mock("@/lib/api/workspaces", () => ({
  checkOpenAIKeyStatus: vi.fn().mockResolvedValue({ has_key: true }),
}));

vi.mock("@/lib/api/base", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api/base")>("@/lib/api/base")),
  apiClient: { get: vi.fn().mockResolvedValue({ version: "9.9.9" }) },
}));

vi.mock("@/components/workspaces/WorkspaceSwitcher", () => ({
  WorkspaceSwitcher: () => <div data-testid="workspace-switcher" />,
}));

vi.mock("@/components/icons/KaguraLogo", () => ({
  KaguraLogo: () => <svg data-testid="kagura-logo" />,
}));

let mockFeatures: Record<string, boolean> | null = { beta_invites: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

import { BETA_INVITE_CARD_DISMISS_KEY } from "@/components/beta-invites/InviteFriendCard";
import { Sidebar } from "./Sidebar";

const SUMMARY = { quota: 4, used: 1, remaining: 3, invites: [] };

const card = () => screen.queryByRole("button", { name: /card\.title/ });
const menuEntry = () => screen.queryByRole("menuitem", { name: /^menuEntry/ });

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  mockFeatures = { beta_invites: true };
  mockUser.role = "user";
  mockGetMine.mockResolvedValue(SUMMARY);
});

describe("Sidebar beta invites — feature off", () => {
  it.each([
    ["the flag is off", { beta_invites: false, byok: true }],
    ["an older backend sends no flag", { byok: true }],
    ["the flags are still loading", null],
  ])("no card, no menu entry, no request when %s", async (_label, features) => {
    mockFeatures = features;
    render(<Sidebar />);
    // Let every mount effect settle before asserting an absence.
    await screen.findByText("Test User");
    await waitFor(() => expect(card()).toBeNull());
    expect(menuEntry()).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mockGetMine).not.toHaveBeenCalled();
  });
});

describe("Sidebar beta invites — feature on", () => {
  it("shows the card directly above the account menu, and the counted entry", async () => {
    render(<Sidebar />);
    await waitFor(() => expect(card()).not.toBeNull());
    expect(mockGetMine).toHaveBeenCalledTimes(1);

    expect(menuEntry()).toHaveTextContent(
      'menuEntryWithCount:{"used":1,"quota":4}',
    );

    // After the nav, before the account trigger.
    const trigger = screen.getByText("Test User");
    const nav = screen.getByRole("navigation");
    expect(
      nav.compareDocumentPosition(card()!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      card()!.compareDocumentPosition(trigger) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("puts the entry right after Profile Settings", async () => {
    render(<Sidebar />);
    await waitFor(() => expect(menuEntry()).not.toBeNull());
    const items = screen.getAllByRole("menuitem");
    const profile = items.findIndex(
      (el) => el.textContent === "profileSettings",
    );
    expect(profile).toBeGreaterThanOrEqual(0);
    expect(items[profile + 1]).toBe(menuEntry());
  });

  it("renders no card while the summary is pending; the entry has no counter yet", async () => {
    mockGetMine.mockReturnValue(new Promise(() => {}));
    render(<Sidebar />);
    await waitFor(() => expect(mockGetMine).toHaveBeenCalledTimes(1));
    expect(card()).toBeNull();
    expect(menuEntry()).toHaveTextContent(/^menuEntry$/);
  });

  it("at the cap: no card, the entry reads 4/4", async () => {
    mockGetMine.mockResolvedValue({ ...SUMMARY, used: 4, remaining: 0 });
    render(<Sidebar />);
    await waitFor(() =>
      expect(menuEntry()).toHaveTextContent(
        'menuEntryWithCount:{"used":4,"quota":4}',
      ),
    );
    expect(card()).toBeNull();
  });

  it("dismissed: no card, but the entry still opens the dialog", async () => {
    window.localStorage.setItem(BETA_INVITE_CARD_DISMISS_KEY, "true");
    render(<Sidebar />);
    await waitFor(() =>
      expect(menuEntry()).toHaveTextContent(/menuEntryWithCount/),
    );
    expect(card()).toBeNull();

    fireEvent.click(menuEntry()!);
    expect(
      await screen.findByRole("dialog", { name: "dialog.title" }),
    ).toBeVisible();
  });

  it("admin: the card is available and the entry shows no denominator", async () => {
    mockUser.role = "admin";
    mockGetMine.mockResolvedValue({
      quota: null,
      used: 12,
      remaining: null,
      invites: [],
    });
    render(<Sidebar />);
    await waitFor(() => expect(card()).not.toBeNull());
    expect(menuEntry()).toHaveTextContent(/^menuEntry$/);
  });

  it("clicking the card opens the dialog", async () => {
    render(<Sidebar />);
    await waitFor(() => expect(card()).not.toBeNull());
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(card()!);
    expect(
      await screen.findByRole("dialog", { name: "dialog.title" }),
    ).toBeVisible();
  });
});
