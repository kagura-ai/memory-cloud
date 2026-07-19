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
const mockUpdateConnectorSettings = vi.fn();
vi.mock("@/lib/api/workspace-connectors", () => ({
  listConnectors: (...args: unknown[]) => mockListConnectors(...args),
  listAvailableWorkerApps: (...args: unknown[]) =>
    mockListAvailableWorkerApps(...args),
  deleteConnector: vi.fn(),
  createConnector: (...args: unknown[]) => mockCreateConnector(...args),
  updateConnectorRuntime: (...args: unknown[]) =>
    mockUpdateConnectorRuntime(...args),
  updateConnectorSettings: (...args: unknown[]) =>
    mockUpdateConnectorSettings(...args),
  getSlackPendingInstall: vi.fn(),
  slackInstallUrl: () => "https://slack.example/install",
}));

const mockRouterReplace = vi.fn();
const mockSearchParamsGet = vi.fn<(key: string) => string | null>();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: mockRouterReplace }),
  useSearchParams: () => ({
    get: (key: string) => mockSearchParamsGet(key),
  }),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: (...args: unknown[]) => mockToast(...args) }),
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
  // clearAllMocks does NOT reset implementations — re-arm defaults here so a
  // per-test mockResolvedValue never leaks into later tests (#1376 review).
  mockUpdateConnectorSettings.mockReset();
  mockSearchParamsGet.mockReturnValue(null);
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
  it("shows a cancelled notice and strips slack_error=cancelled (#1375)", async () => {
    setWorkspace("admin");
    mockSearchParamsGet.mockImplementation((key: string) =>
      key === "slack_error" ? "cancelled" : null,
    );
    render(<ConnectorsPage />);

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "slackCancelledTitle",
          description: "slackCancelledDesc",
        }),
      ),
    );
    // Informational, not destructive — the user chose to cancel.
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: "destructive" }),
    );
    // Param stripped so refresh/back doesn't re-toast.
    expect(mockRouterReplace).toHaveBeenCalledWith(
      "/workspace/integrations/connectors",
    );
  });

  it("shows a destructive failed notice for slack_error=failed (#1375)", async () => {
    setWorkspace("admin");
    mockSearchParamsGet.mockImplementation((key: string) =>
      key === "slack_error" ? "failed" : null,
    );
    render(<ConnectorsPage />);

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "destructive",
          title: "slackFailedTitle",
          description: "slackFailedDesc",
        }),
      ),
    );
    expect(mockRouterReplace).toHaveBeenCalledWith(
      "/workspace/integrations/connectors",
    );
  });

  it("shows a destructive expired notice for slack_error=expired (#1381)", async () => {
    setWorkspace("admin");
    mockSearchParamsGet.mockImplementation((key: string) =>
      key === "slack_error" ? "expired" : null,
    );
    render(<ConnectorsPage />);

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "destructive",
          title: "slackExpiredTitle",
          description: "slackExpiredDesc",
        }),
      ),
    );
    expect(mockRouterReplace).toHaveBeenCalledWith(
      "/workspace/integrations/connectors",
    );
  });

  it("renders vend-settings indicators on the connector row (#1376)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      {
        connector_id: "connector-1",
        connector_type: "slack",
        app_key: "default",
        resource_id: "slack-t01",
        context_id: "context-1",
        config_version: 3,
        created_at: "2026-07-19T00:00:00Z",
        created_by: "user-1",
        runtime: { vision_enabled: true },
        channel_ids: ["C1", "C2"],
        locale: "ja",
        litellm_virtual_key_id: null,
        llm_config_present: true,
      },
    ]);

    render(<ConnectorsPage />);

    // i18n mock returns keys; params are dropped.
    expect(await screen.findByText("channelsCount")).toBeInTheDocument();
    expect(screen.getByText("llmBound")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "editSettings" }),
    ).toBeInTheDocument();
  });

  it("submits only changed settings with the version guard (#1376)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      {
        connector_id: "connector-1",
        connector_type: "slack",
        app_key: "default",
        resource_id: "slack-t01",
        context_id: "context-1",
        config_version: 3,
        created_at: "2026-07-19T00:00:00Z",
        created_by: "user-1",
        runtime: { vision_enabled: true },
        channel_ids: ["C1"],
        locale: null,
        litellm_virtual_key_id: null,
        llm_config_present: false,
      },
    ]);
    mockUpdateConnectorSettings.mockResolvedValue({
      connector_id: "connector-1",
      channel_ids: ["C1", "C2"],
      litellm_virtual_key_id: null,
      llm_config_present: false,
      locale: null,
      config_version: 4,
    });

    render(<ConnectorsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );
    const channelsInput = await screen.findByLabelText("channelsLabel");
    fireEvent.change(channelsInput, { target: { value: "C1, C2" } });
    fireEvent.click(screen.getByRole("button", { name: "settingsSave" }));

    await waitFor(() =>
      expect(mockUpdateConnectorSettings).toHaveBeenCalledWith(
        "connector-1",
        { channel_ids: ["C1", "C2"] },
        3,
      ),
    );
  });

  it("treats llm-clear on an unbound connector as no-change (#1376)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      {
        connector_id: "connector-1",
        connector_type: "slack",
        app_key: "default",
        resource_id: "slack-t01",
        context_id: "context-1",
        config_version: 3,
        created_at: "2026-07-19T00:00:00Z",
        created_by: "user-1",
        runtime: { vision_enabled: true },
        channel_ids: ["C1"],
        locale: null,
        litellm_virtual_key_id: null,
        llm_config_present: false,
      },
    ]);

    render(<ConnectorsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );
    // Tick "clear LLM" with nothing bound and change nothing else.
    fireEvent.click(await screen.findByRole("switch", { name: "llmClear" }));
    fireEvent.click(screen.getByRole("button", { name: "settingsSave" }));

    // No-op guard: nothing to clear → no PATCH, inline notice instead.
    expect(await screen.findByText("noChanges")).toBeInTheDocument();
    expect(mockUpdateConnectorSettings).not.toHaveBeenCalled();
  });

  it("does not toast slack_error for non-admins (#1375)", async () => {
    setWorkspace("member");
    mockSearchParamsGet.mockImplementation((key: string) =>
      key === "slack_error" ? "cancelled" : null,
    );
    render(<ConnectorsPage />);

    expect(
      await screen.findByText("errors.forbiddenWorkspace"),
    ).toBeInTheDocument();
    expect(mockToast).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
  });

  it("keeps the connectors list when available-apps fails (#1360)", async () => {
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
        runtime: { vision_enabled: true },
      },
    ]);
    mockListAvailableWorkerApps.mockRejectedValue(new Error("apps down"));

    render(<ConnectorsPage />);

    // Primary content still renders (the i18n mock drops params, so
    // match the row's static bits rather than the resource id).
    expect(await screen.findByText("slack")).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "visionEnabledFor" }),
    ).toBeInTheDocument();
    // ...the manual-bind form degrades away...
    expect(screen.queryByText("manualBindTitle")).not.toBeInTheDocument();
    // ...and the degradation is surfaced, not silent.
    expect(screen.getByText("apps down")).toBeInTheDocument();
  });
});
