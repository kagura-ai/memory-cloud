/**
 * Connectors page — client-side RBAC gate (#903).
 *
 * Covers: non-admin (member/viewer) sees the forbidden banner and the
 * admin-only list call is never fired; admin sees the management UI and the
 * list loads; loading resolves before gating (no admin-UI flash); a missing
 * workspace shows the "no workspace selected" banner rather than the role
 * banner.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConnectorsPage from "./page";

const mockListConnectors = vi.fn();
const mockListAvailableWorkerApps = vi.fn();
const mockCreateConnector = vi.fn();
const mockUpdateConnectorRuntime = vi.fn();
vi.mock("@/lib/api/workspace-connectors", () => ({
  listConnectors: (...args: unknown[]) => mockListConnectors(...args),
  listAvailableWorkerApps: (...args: unknown[]) =>
    mockListAvailableWorkerApps(...args),
  deleteConnector: vi.fn(),
  createConnector: (...args: unknown[]) => mockCreateConnector(...args),
  updateConnectorRuntime: (...args: unknown[]) =>
    mockUpdateConnectorRuntime(...args),
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
  mockListAvailableWorkerApps.mockResolvedValue([]);
  mockCreateConnector.mockResolvedValue({
    connector_id: "connector-1",
    connector_type: "slack",
    app_key: "sales",
    resource_id: "slack-sales-t01",
    context_id: "context-1",
    token_id: 1,
    token: "resource-token",
    kmc_api_key: "kmc-key",
    quota_events_per_hour: 1000,
    idempotency_key_prefix: "connector-1:",
  });
  mockUpdateConnectorRuntime.mockImplementation(
    async (connectorId: string, runtime: Record<string, unknown>) => ({
      connector_id: connectorId,
      runtime,
      config_version: 2,
    }),
  );
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
      expect(mockListAvailableWorkerApps).not.toHaveBeenCalled();
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
    expect(mockListAvailableWorkerApps).not.toHaveBeenCalled();
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
    expect(mockListAvailableWorkerApps).not.toHaveBeenCalled();
  });

  it("binds an active app identity and clears the installation bot token", async () => {
    setWorkspace("admin");
    mockListAvailableWorkerApps.mockResolvedValue([
      {
        platform: "slack",
        app_key: "sales",
        display_name: "Sales Slack App",
      },
    ]);

    render(<ConnectorsPage />);

    await screen.findByText("manualBindTitle");
    fireEvent.change(screen.getByLabelText("manualTeamId"), {
      target: { value: "T01" },
    });
    const tokenInput = screen.getByLabelText("manualBotToken");
    fireEvent.change(tokenInput, { target: { value: "xoxb-install-token" } });
    fireEvent.click(screen.getByRole("button", { name: "manualBind" }));

    await waitFor(() =>
      expect(mockCreateConnector).toHaveBeenCalledWith(
        expect.objectContaining({
          connector_type: "slack",
          app_key: "sales",
          external_team_id: "T01",
          oauth_tokens: { bot_token: "xoxb-install-token" },
          resource_id: "slack-sales-t01",
          auto_create_context_name: "slack-sales-t01",
        }),
      ),
    );
    await waitFor(() => expect(tokenInput).toHaveValue(""));
  });

  it("updates the tenant-local vision kill-switch from the connector row", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      {
        connector_id: "connector-1",
        connector_type: "slack",
        app_key: "default",
        resource_id: "slack-t01",
        context_id: "context-1",
        config_version: 1,
        created_at: "2026-07-18T00:00:00Z",
        created_by: "user-1",
        runtime: {
          vision_enabled: true,
        },
      },
    ]);

    render(<ConnectorsPage />);

    const toggle = await screen.findByRole("switch", {
      name: "visionEnabledFor",
    });
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(mockUpdateConnectorRuntime).toHaveBeenCalledWith(
        "connector-1",
        expect.objectContaining({ vision_enabled: false }),
        // The snapshot's config_version rides along as the
        // optimistic-concurrency guard (server 409s on staleness).
        1,
      ),
    );
  });
});
