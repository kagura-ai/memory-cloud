/**
 * Connectors page — client-side RBAC gate (#903).
 *
 * Covers: non-admin (member/viewer) sees the forbidden banner and the
 * admin-only list call is never fired; admin sees the management UI and the
 * list loads; loading resolves before gating (no admin-UI flash); a missing
 * workspace shows the "no workspace selected" banner rather than the role
 * banner.
 */
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConnectorsPage from "./page";

const mockListConnectors = vi.fn();
vi.mock("@/lib/api/workspace-connectors", () => ({
  listConnectors: (...args: unknown[]) => mockListConnectors(...args),
  deleteConnector: vi.fn(),
  createConnector: vi.fn(),
  getSlackPendingInstall: vi.fn(),
  slackInstallUrl: () => "https://slack.example/install",
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => ({ get: () => null }),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/hooks/useCopyFeedback", () => ({
  useCopyFeedback: () => ({ isCopied: () => false, copyToTarget: vi.fn() }),
}));

function setWorkspace(role: string | undefined, overrides = {}) {
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: role ? { id: "ws-1", current_user_role: role } : null,
    currentWorkspaceId: "ws-1",
    loading: false,
    ...overrides,
  });
}

beforeEach(() => {
  mockListConnectors.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ConnectorsPage RBAC gate", () => {
  it.each(["member", "viewer"])(
    "shows forbidden banner and never lists connectors for %s",
    async (role) => {
      setWorkspace(role);
      render(<ConnectorsPage />);

      expect(
        await screen.findByText("errors.forbiddenWorkspace"),
      ).toBeInTheDocument();
      // The admin-only list call must not fire for non-admins.
      expect(mockListConnectors).not.toHaveBeenCalled();
      // No connect action surfaced.
      expect(screen.queryByText("connectSlack")).not.toBeInTheDocument();
    },
  );

  it.each(["admin", "owner"])(
    "renders management UI and loads connectors for %s",
    async (role) => {
      setWorkspace(role);
      render(<ConnectorsPage />);

      expect(await screen.findByText("connectSlack")).toBeInTheDocument();
      await waitFor(() => expect(mockListConnectors).toHaveBeenCalled());
      expect(
        screen.queryByText("errors.forbiddenWorkspace"),
      ).not.toBeInTheDocument();
    },
  );

  it("shows a loading state (no admin-UI flash) while the workspace resolves", () => {
    setWorkspace(undefined, { currentWorkspace: null, loading: true });
    render(<ConnectorsPage />);

    expect(screen.queryByText("connectSlack")).not.toBeInTheDocument();
    expect(
      screen.queryByText("errors.forbiddenWorkspace"),
    ).not.toBeInTheDocument();
    expect(mockListConnectors).not.toHaveBeenCalled();
  });

  it("distinguishes no-workspace from wrong-role", async () => {
    setWorkspace(undefined, {
      currentWorkspace: null,
      currentWorkspaceId: null,
    });
    render(<ConnectorsPage />);

    expect(
      await screen.findByText("errors.noWorkspaceSelected"),
    ).toBeInTheDocument();
    expect(mockListConnectors).not.toHaveBeenCalled();
  });
});
