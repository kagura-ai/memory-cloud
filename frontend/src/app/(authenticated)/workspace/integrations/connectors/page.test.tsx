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
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConnectorsPage from "./page";

const mockListConnectors = vi.fn();
const mockListAvailableWorkerApps = vi.fn();
const mockCreateConnector = vi.fn();
const mockUpdateConnectorRuntime = vi.fn();
const mockUpdateConnectorSettings = vi.fn();
const mockGetSlackPendingInstall = vi.fn();
const mockListConnectorChannels = vi.fn();
vi.mock("@/lib/api/workspace-connectors", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/api/workspace-connectors")>();
  return {
    // Pure helpers — use the real rules, not mocks.
    connectorReadiness: actual.connectorReadiness,
    connectorDisplayName: actual.connectorDisplayName,
    listConnectorChannels: (...args: unknown[]) =>
      mockListConnectorChannels(...args),
    listConnectors: (...args: unknown[]) => mockListConnectors(...args),
    listAvailableWorkerApps: (...args: unknown[]) =>
      mockListAvailableWorkerApps(...args),
    deleteConnector: vi.fn(),
    createConnector: (...args: unknown[]) => mockCreateConnector(...args),
    updateConnectorRuntime: (...args: unknown[]) =>
      mockUpdateConnectorRuntime(...args),
    updateConnectorSettings: (...args: unknown[]) =>
      mockUpdateConnectorSettings(...args),
    getSlackPendingInstall: (...args: unknown[]) =>
      mockGetSlackPendingInstall(...args),
    slackInstallUrl: () => "https://slack.example/install",
  };
});

// #1409: the create dialog offers an existing-context picker sourced from
// getContexts(); mock it like the connector helpers.
const mockGetContexts = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...args: unknown[]) => mockGetContexts(...args),
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

// #1426: managed (hosted SaaS) mode flag. Default {} = non-managed so existing
// tests are unaffected; managed-mode tests override.
const mockUseSystemFeatures = vi.fn();
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockUseSystemFeatures(),
}));

// #1399: the fold/label tests differ only by llm_config_present, so build the
// stored-connector row from one factory instead of re-inlining every field.
function makeConnector(overrides: Record<string, unknown> = {}) {
  return {
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
    // #1449: ingest outcome for the row.
    last_memory_at: null,
    memories_last_7d: 0,
    ingest_context_shared: false,
    ...overrides,
  };
}

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
  mockUseSystemFeatures.mockReturnValue({}); // #1426: non-managed by default
  // #1391: default the channel list to unavailable so the picker falls back to
  // the manual-ID lane (the shape existing channel tests exercise). The
  // select-mode test overrides with a resolved page.
  mockListConnectorChannels.mockRejectedValue(new Error("not-mocked"));
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
  // #1409: default to a no-op pending install + empty context list; the
  // create-dialog tests arm these per-test.
  mockGetSlackPendingInstall.mockResolvedValue({
    team_id: "T01",
    team_name: "Acme",
    installing_admin_user_id: "user-1",
    app_key: "default",
  });
  mockGetContexts.mockResolvedValue({ contexts: [], total: 0 });
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
      expect(screen.queryByText("connectProvider")).not.toBeInTheDocument();
    },
  );

  it.each(["admin", "owner"])(
    "renders management UI and loads connectors for %s",
    async (role) => {
      setWorkspace(role);
      render(<ConnectorsPage />);

      expect(await screen.findByText("connectProvider")).toBeInTheDocument();
      await waitFor(() => expect(mockListConnectors).toHaveBeenCalled());
      expect(
        screen.queryByText("errors.forbiddenWorkspace"),
      ).not.toBeInTheDocument();
    },
  );

  it("shows a loading state (no admin-UI flash) while the workspace resolves", () => {
    setWorkspace(undefined, { currentWorkspace: null, loading: true });
    render(<ConnectorsPage />);

    expect(screen.queryByText("connectProvider")).not.toBeInTheDocument();
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

  it("surfaces the post-connect next-steps checklist after creation (#1426)", async () => {
    setWorkspace("admin");
    mockListAvailableWorkerApps.mockResolvedValue([
      { platform: "slack", app_key: "sales", display_name: "Sales Slack App" },
    ]);

    render(<ConnectorsPage />);

    await screen.findByText("manualBindTitle");
    fireEvent.change(screen.getByLabelText("manualTeamId"), {
      target: { value: "T01" },
    });
    fireEvent.change(screen.getByLabelText("manualBotToken"), {
      target: { value: "xoxb-install-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "manualBind" }));

    // The success dialog must guide the two remaining Slack-side actions
    // (invite bot + select channels) — "created" is not "done". The dialog is
    // the same JSX for both the OAuth and manual-bind create paths, so covering
    // one entry point covers the shared checklist.
    expect(await screen.findByText("nextStepsTitle")).toBeInTheDocument();
    expect(screen.getByText("nextStepInviteBot")).toBeInTheDocument();
    expect(screen.getByText("nextStepSelectChannels")).toBeInTheDocument();
  });

  it("managed mode hides the BYO form and drops the LLM requirement (#1426)", async () => {
    setWorkspace("admin");
    mockUseSystemFeatures.mockReturnValue({ managed_connectors: true });
    mockListAvailableWorkerApps.mockResolvedValue([
      { platform: "slack", app_key: "sales", display_name: "Sales Slack App" },
    ]);
    // Channels set, no per-connector LLM: un-vendable under self-host rules,
    // but ready under managed (platform provides the LLM).
    mockListConnectors.mockResolvedValue([
      makeConnector({ channel_ids: ["C1"], llm_config_present: false }),
    ]);

    render(<ConnectorsPage />);
    await screen.findByText("connectProvider");

    // BYO "link existing app" form is hidden even though app identities exist.
    expect(screen.queryByText("manualBindTitle")).not.toBeInTheDocument();
    // Row reads ready (not 要設定) and no missing-LLM affordance.
    expect(await screen.findByText("runningBadge")).toBeInTheDocument();
    expect(screen.queryByText("llmNotBound")).not.toBeInTheDocument();

    // Settings dialog shows the platform-managed LLM note, not the LLM inputs.
    fireEvent.click(screen.getByRole("button", { name: "editSettings" }));
    expect(await screen.findByText("llmManagedNote")).toBeInTheDocument();
    expect(screen.queryByText("llmDelete")).not.toBeInTheDocument();
  });

  it("picks channels from the server list and saves channel_ids (#1391)", async () => {
    setWorkspace("admin");
    mockListConnectorChannels.mockResolvedValue({
      channels: [
        { id: "C1", name: "general", is_private: false, is_member: true },
        { id: "C2", name: "random", is_private: false, is_member: true },
      ],
      next_cursor: null,
    });
    mockListConnectors.mockResolvedValue([
      makeConnector({ channel_ids: [], llm_config_present: true }),
    ]);
    mockUpdateConnectorSettings.mockResolvedValue({
      connector_id: "connector-1",
      channel_ids: ["C2"],
      litellm_virtual_key_id: null,
      llm_config_present: true,
      locale: null,
      config_version: 4,
      context_id: "context-1",
    });

    render(<ConnectorsPage />);
    await screen.findByText("connectProvider");
    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );

    // Picker loaded the server list — select #random, then save.
    fireEvent.click(await screen.findByRole("button", { name: /random/ }));
    fireEvent.click(screen.getByRole("button", { name: "settingsSave" }));

    await waitFor(() =>
      expect(mockUpdateConnectorSettings).toHaveBeenCalledWith(
        "connector-1",
        expect.objectContaining({ channel_ids: ["C2"] }),
        expect.anything(),
      ),
    );
  });

  it("flags channels the bot has not joined, and warns once one is selected (#1451)", async () => {
    setWorkspace("admin");
    mockListConnectorChannels.mockResolvedValue({
      channels: [
        { id: "C1", name: "joined", is_private: false, is_member: true },
        { id: "C2", name: "not-joined", is_private: false, is_member: false },
      ],
      next_cursor: null,
    });
    mockListConnectors.mockResolvedValue([
      makeConnector({ channel_ids: [], llm_config_present: true }),
    ]);

    render(<ConnectorsPage />);
    await screen.findByText("connectProvider");
    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );

    // The badge marks the un-joined channel — and only that one.
    expect(
      await screen.findByText("channelsBotNotInChannel"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("channelsBotNotInChannel")).toHaveLength(1);

    // Nothing is selected yet, so there is nothing to warn about.
    expect(
      screen.queryByText("channelsNotJoinedWarning"),
    ).not.toBeInTheDocument();

    // Selecting the joined channel still warns about nothing.
    fireEvent.click(await screen.findByRole("button", { name: /joined$/ }));
    expect(
      screen.queryByText("channelsNotJoinedWarning"),
    ).not.toBeInTheDocument();

    // Selecting the un-joined one surfaces the warning.
    fireEvent.click(screen.getByRole("button", { name: /not-joined/ }));
    expect(
      await screen.findByText("channelsNotJoinedWarning"),
    ).toBeInTheDocument();
  });

  it("says membership is unverified for selections on unloaded pages (#1451)", async () => {
    setWorkspace("admin");
    // A saved selection that lives on a later Slack page: not in the loaded
    // list, and more pages exist. Staying silent here would read as an
    // all-clear — the exact failure mode #1451 is about.
    mockListConnectorChannels.mockResolvedValue({
      channels: [
        { id: "C1", name: "general", is_private: false, is_member: true },
      ],
      next_cursor: "PAGE2",
    });
    mockListConnectors.mockResolvedValue([
      makeConnector({ channel_ids: ["C450"], llm_config_present: true }),
    ]);

    render(<ConnectorsPage />);
    await screen.findByText("connectProvider");
    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );

    expect(
      await screen.findByText("channelsMembershipUnverified"),
    ).toBeInTheDocument();
    // Nothing is *known* to be un-joined, so the harder warning stays off.
    expect(
      screen.queryByText("channelsNotJoinedWarning"),
    ).not.toBeInTheDocument();
  });

  it("does not claim unverified when the whole list is loaded (#1451)", async () => {
    setWorkspace("admin");
    // next_cursor null → we have seen every public channel. A leftover id is a
    // private channel or manual entry, whose membership this proxy cannot know
    // either way, so neither message applies.
    mockListConnectorChannels.mockResolvedValue({
      channels: [
        { id: "C1", name: "general", is_private: false, is_member: true },
      ],
      next_cursor: null,
    });
    mockListConnectors.mockResolvedValue([
      makeConnector({ channel_ids: ["CPRIVATE"], llm_config_present: true }),
    ]);

    render(<ConnectorsPage />);
    await screen.findByText("connectProvider");
    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );

    // The unlisted chip is still rendered (the save must not drop the id)…
    expect(await screen.findByText("CPRIVATE")).toBeInTheDocument();
    // …but we make no membership claim about it.
    expect(
      screen.queryByText("channelsMembershipUnverified"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("channelsNotJoinedWarning"),
    ).not.toBeInTheDocument();
  });

  it("states ingest activity as fact, without grading it (#1449)", async () => {
    setWorkspace("admin");
    // A connector that has not written in days — the exact shape of the
    // 2026-07-21..27 outage, which every screen rendered as normal.
    mockListConnectors.mockResolvedValue([
      makeConnector({
        last_memory_at: "2026-07-19T00:00:00Z",
        memories_last_7d: 0,
      }),
    ]);

    render(<ConnectorsPage />);

    expect(await screen.findByText(/ingestLastWrite/)).toBeInTheDocument();
    expect(screen.queryByText("ingestNeverWritten")).not.toBeInTheDocument();
    // Silence was the bug; a permanently-red row would be the next one. The
    // fact is shown, the judgement is left to the operator — so no error
    // channel (banner/toast) fires for a quiet connector.
    expect(mockToast).not.toHaveBeenCalled();
    expect(
      screen.queryByText("errors.forbiddenWorkspace"),
    ).not.toBeInTheDocument();
  });

  it("distinguishes never-written from written-but-quiet (#1449)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      makeConnector({ last_memory_at: null, memories_last_7d: 0 }),
    ]);

    render(<ConnectorsPage />);

    expect(await screen.findByText(/ingestNeverWritten/)).toBeInTheDocument();
  });

  it("says so when the figures cover a shared context (#1449)", async () => {
    setWorkspace("admin");
    // Two connectors on one context: the numbers are the pair's combined
    // traffic, so a dead connector could otherwise read as healthy off its
    // sibling — the one way these figures mislead.
    mockListConnectors.mockResolvedValue([
      makeConnector({
        last_memory_at: "2026-07-27T00:00:00Z",
        memories_last_7d: 12,
        ingest_context_shared: true,
      }),
    ]);

    render(<ConnectorsPage />);

    expect(await screen.findByText(/ingestSharedContext/)).toBeInTheDocument();
  });

  it("keeps the exact timestamp available for log correlation (#1449)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      makeConnector({ last_memory_at: "2026-07-19T03:04:05Z" }),
    ]);

    render(<ConnectorsPage />);

    // "6 days ago" scans fast but cannot be lined up against an outage window;
    // the absolute UTC time rides along in the title (review finding).
    const line = await screen.findByText(/ingestLastWrite/);
    expect(line).toHaveAttribute("title", expect.stringContaining("2026"));
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

  it("renders human names first on the row and demotes UUIDs (#1389)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      {
        connector_id: "connector-1",
        connector_type: "slack",
        app_key: "default",
        resource_id: "slack-t01",
        context_id: "context-1",
        config_version: 3,
        created_at: "2026-07-20T00:00:00Z",
        created_by: "user-1",
        runtime: { vision_enabled: true },
        channel_ids: ["C1"],
        locale: "ja",
        litellm_virtual_key_id: null,
        llm_config_present: true,
        display_name: "Sales Slack / T0123ABC",
        external_team_id: "T0123ABC",
        context_name: "slack-sales",
      },
    ]);

    render(<ConnectorsPage />);

    // Row title is the human-readable name, not the connector type.
    expect(
      await screen.findByText("Sales Slack / T0123ABC"),
    ).toBeInTheDocument();
    // Context shown by name (i18n mock drops params → key text).
    expect(screen.getByText("contextBoundName")).toBeInTheDocument();
    // The context UUID is demoted behind a copy affordance.
    expect(
      screen.getByRole("button", { name: "copyContextId" }),
    ).toBeInTheDocument();
    // Aggregate readiness badge: channels + LLM stored → active.
    expect(screen.getByText("runningBadge")).toBeInTheDocument();
    expect(screen.queryByText("needsSetupBadge")).not.toBeInTheDocument();
  });

  it("marks an un-vendable row 要設定 and opens settings from a missing badge (#1389)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      {
        connector_id: "connector-1",
        connector_type: "slack",
        app_key: "default",
        resource_id: "slack-t01",
        context_id: "context-1",
        config_version: 3,
        created_at: "2026-07-20T00:00:00Z",
        created_by: "user-1",
        runtime: { vision_enabled: true },
        channel_ids: [],
        locale: null,
        litellm_virtual_key_id: null,
        llm_config_present: false,
        display_name: null,
        external_team_id: "T0123ABC",
        context_name: null,
      },
    ]);

    render(<ConnectorsPage />);

    // Fallback title chain: no display_name → team id.
    expect(await screen.findByText("T0123ABC")).toBeInTheDocument();
    expect(screen.getByText("needsSetupBadge")).toBeInTheDocument();
    // A missing badge is an affordance: clicking opens the settings dialog.
    fireEvent.click(screen.getByRole("button", { name: "fixChannels" }));
    expect(await screen.findByText("settingsTitle")).toBeInTheDocument();
  });

  it("renders the provider picker with disabled coming-soon providers (#1389)", async () => {
    setWorkspace("admin");
    render(<ConnectorsPage />);

    // Enabled provider keeps a live connect CTA…
    const slackCta = await screen.findAllByRole("button", {
      name: /connectProvider/,
    });
    expect(slackCta.length).toBeGreaterThan(0);
    // …while Discord / Teams render as disabled coming-soon affordances.
    const discord = screen.getByRole("button", { name: /Discord/ });
    const teams = screen.getByRole("button", { name: /Microsoft Teams/ });
    expect(discord).toBeDisabled();
    expect(teams).toBeDisabled();
  });

  it("rejects a manual bind token without the xoxb- prefix client-side (#1389)", async () => {
    setWorkspace("admin");
    mockListAvailableWorkerApps.mockResolvedValue([
      { platform: "slack", app_key: "sales", display_name: "Sales Slack App" },
    ]);

    render(<ConnectorsPage />);

    await screen.findByText("manualBindTitle");
    fireEvent.change(screen.getByLabelText("manualTeamId"), {
      target: { value: "T01" },
    });
    fireEvent.change(screen.getByLabelText("manualBotToken"), {
      target: { value: "xoxp-user-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "manualBind" }));

    expect(
      await screen.findByText("manualBotTokenInvalid"),
    ).toBeInTheDocument();
    expect(mockCreateConnector).not.toHaveBeenCalled();
  });

  it("rejects a manual bind team ID without the T prefix client-side (#1389)", async () => {
    setWorkspace("admin");
    mockListAvailableWorkerApps.mockResolvedValue([
      { platform: "slack", app_key: "sales", display_name: "Sales Slack App" },
    ]);

    render(<ConnectorsPage />);

    await screen.findByText("manualBindTitle");
    fireEvent.change(screen.getByLabelText("manualTeamId"), {
      target: { value: "W123" },
    });
    fireEvent.change(screen.getByLabelText("manualBotToken"), {
      target: { value: "xoxb-install-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "manualBind" }));

    expect(await screen.findByText("manualTeamIdInvalid")).toBeInTheDocument();
    expect(mockCreateConnector).not.toHaveBeenCalled();
  });

  it("does not count a litellm-only connector as LLM-bound on the row (#1388)", async () => {
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
        litellm_virtual_key_id: "vk-1",
        llm_config_present: false,
      },
    ]);

    render(<ConnectorsPage />);

    // The virtual key is stored but not vended to the worker: the row
    // must agree with the dialog readiness rule instead of contradicting it.
    expect(await screen.findByText("llmNotBound")).toBeInTheDocument();
    expect(screen.queryByText("llmBound")).not.toBeInTheDocument();
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

  it("hides the LLM delete action when nothing is stored (#1388)", async () => {
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
    await screen.findByLabelText("channelsLabel");
    // No stored bundle → no destructive affordance (and the old clear
    // switch is gone entirely).
    expect(
      screen.queryByRole("button", { name: "llmDelete" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("switch", { name: "llmClear" }),
    ).not.toBeInTheDocument();
  });

  it("deletes the stored LLM config via explicit confirm and refreshes the version snapshot (#1388)", async () => {
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
        llm_config_present: true,
      },
    ]);
    mockUpdateConnectorSettings.mockResolvedValue({
      connector_id: "connector-1",
      channel_ids: ["C1"],
      litellm_virtual_key_id: null,
      llm_config_present: false,
      locale: null,
      config_version: 4,
    });

    render(<ConnectorsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "llmDelete" }));
    // Destructive action goes through an explicit confirm dialog.
    const confirm = await screen.findByRole("alertdialog");
    expect(
      within(confirm).getByText("llmDeleteConfirmTitle"),
    ).toBeInTheDocument();
    fireEvent.click(within(confirm).getByRole("button", { name: "delete" }));

    await waitFor(() =>
      expect(mockUpdateConnectorSettings).toHaveBeenCalledWith(
        "connector-1",
        { llm_config: null },
        3,
      ),
    );
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "llmDeleted" }),
      ),
    );

    // The dialog stays open on a fresh snapshot: a follow-up save must ride
    // the bumped config_version, not 409 on the stale one.
    const channelsInput = await screen.findByLabelText("channelsLabel");
    fireEvent.change(channelsInput, { target: { value: "C1, C2" } });
    fireEvent.click(screen.getByRole("button", { name: "settingsSave" }));
    await waitFor(() =>
      expect(mockUpdateConnectorSettings).toHaveBeenLastCalledWith(
        "connector-1",
        { channel_ids: ["C1", "C2"] },
        4,
      ),
    );
  });

  it("shows the not-ready summary with missing items in the settings dialog (#1388)", async () => {
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
        channel_ids: [],
        locale: null,
        litellm_virtual_key_id: null,
        llm_config_present: false,
      },
    ]);

    render(<ConnectorsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );
    expect(await screen.findByText("notReadySummary")).toBeInTheDocument();
    expect(screen.queryByText("readySummary")).not.toBeInTheDocument();
  });

  it("shows the ready summary once channels and LLM are stored (#1388)", async () => {
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
        locale: "ja",
        litellm_virtual_key_id: null,
        llm_config_present: true,
      },
    ]);

    render(<ConnectorsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );
    expect(await screen.findByText("readySummary")).toBeInTheDocument();
    expect(screen.queryByText("notReadySummary")).not.toBeInTheDocument();
  });

  it("accepts provider+model without an API key and omits api_key from the bundle (#1388)", async () => {
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
      channel_ids: ["C1"],
      litellm_virtual_key_id: null,
      llm_config_present: true,
      locale: null,
      config_version: 4,
    });

    render(<ConnectorsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );
    fireEvent.change(await screen.findByLabelText("llmProvider"), {
      target: { value: "ollama" },
    });
    fireEvent.change(screen.getByLabelText("llmModel"), {
      target: { value: "llama3" },
    });
    fireEvent.click(screen.getByRole("button", { name: "settingsSave" }));

    // api_key stays absent (backend treats it as provider-dependent —
    // e.g. local Ollama has none), matching _validate_llm_config.
    await waitFor(() =>
      expect(mockUpdateConnectorSettings).toHaveBeenCalledWith(
        "connector-1",
        { llm_config: { provider: "ollama", model: "llama3" } },
        3,
      ),
    );
  });

  it("rejects a partial LLM bundle missing the model (#1388)", async () => {
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
    fireEvent.change(await screen.findByLabelText("llmProvider"), {
      target: { value: "openai" },
    });
    fireEvent.click(screen.getByRole("button", { name: "settingsSave" }));

    expect(await screen.findByText("llmIncomplete")).toBeInTheDocument();
    expect(mockUpdateConnectorSettings).not.toHaveBeenCalled();
  });

  it("collapses the stored LLM fields behind a replace fold with delete outside (#1399)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      makeConnector({ llm_config_present: true }),
    ]);

    render(<ConnectorsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );
    await screen.findByLabelText("channelsLabel");
    // Configured: the write-only inputs sit inside the closed fold (present
    // in the DOM per the <details> model, but not visible)…
    expect(screen.getByLabelText("llmProvider")).not.toBeVisible();
    expect(screen.getByLabelText("llmModel")).not.toBeVisible();
    expect(screen.getByLabelText("llmApiKey")).not.toBeVisible();
    // …while the destructive action stays one click away, outside the fold.
    expect(screen.getByRole("button", { name: "llmDelete" })).toBeVisible();
    // Expanding the replace fold reveals the fields.
    fireEvent.click(screen.getByText("llmReplaceToggle"));
    expect(screen.getByLabelText("llmProvider")).toBeVisible();
    expect(screen.getByLabelText("llmModel")).toBeVisible();
    expect(screen.getByLabelText("llmApiKey")).toBeVisible();
  });

  it("shows LLM fields directly with visible labels when nothing is stored (#1399)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      makeConnector({ llm_config_present: false }),
    ]);

    render(<ConnectorsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "editSettings" }),
    );
    await screen.findByLabelText("channelsLabel");
    // Unconfigured: no replace fold — the goal is to get the fields filled.
    expect(screen.queryByText("llmReplaceToggle")).not.toBeInTheDocument();
    expect(screen.getByLabelText("llmProvider")).toBeVisible();
    // Each field carries a visible <label> (placeholder-as-label is gone) —
    // getByText only matches rendered text, never an aria-label attribute.
    expect(screen.getByText("llmProvider").tagName).toBe("LABEL");
    expect(screen.getByText("llmModel").tagName).toBe("LABEL");
    expect(screen.getByText("llmApiKey").tagName).toBe("LABEL");
  });

  it("opens the dialog with LLM fields directly visible from the fixLlm badge (#1399)", async () => {
    setWorkspace("admin");
    mockListConnectors.mockResolvedValue([
      makeConnector({ llm_config_present: false }),
    ]);

    render(<ConnectorsPage />);

    // The focus-steering lane and the fold never collide: fixLlm only
    // renders when the LLM config is missing, and a missing config means
    // the fields render unfolded (#1399 invariant).
    fireEvent.click(await screen.findByRole("button", { name: "fixLlm" }));
    expect(await screen.findByText("settingsTitle")).toBeInTheDocument();
    expect(screen.getByLabelText("llmProvider")).toBeVisible();
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
    // match the row's static bits rather than the resource id). The
    // type fallback title is capitalized by connectorDisplayName (#1389).
    expect(await screen.findByText("Slack")).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "visionEnabledFor" }),
    ).toBeInTheDocument();
    // ...the manual-bind form degrades away...
    expect(screen.queryByText("manualBindTitle")).not.toBeInTheDocument();
    // ...and the degradation is surfaced, not silent.
    expect(screen.getByText("apps down")).toBeInTheDocument();
  });

  // #1409: the create dialog opens after the Slack OAuth callback returns
  // ?slack_install=<handle>. Arm the search param + pending-install lookup.
  function armInstall() {
    mockSearchParamsGet.mockImplementation((key: string) =>
      key === "slack_install" ? "handle-1" : null,
    );
  }

  it("connects a Slack connector to an existing context (context_id, no auto-create) (#1409)", async () => {
    setWorkspace("admin");
    armInstall();
    mockGetContexts.mockResolvedValue({
      contexts: [
        { id: "ctx-existing", name: "slack-kagura-ai", display_name: null },
      ],
      total: 1,
    });

    render(<ConnectorsPage />);

    // Dialog opens defaulted to connect-existing; submit uses the preselected
    // first context without interacting with the Radix Select popover.
    fireEvent.click(
      await screen.findByRole("button", { name: "createConnector" }),
    );

    await waitFor(() =>
      expect(mockCreateConnector).toHaveBeenCalledWith(
        expect.objectContaining({ context_id: "ctx-existing" }),
      ),
    );
    // Exactly one write-target field: existing mode must NOT auto-create.
    const arg = mockCreateConnector.mock.calls[0][0];
    expect(arg).not.toHaveProperty("auto_create_context_name");
  });

  it("creates a new context when switched to create-new mode (auto_create_context_name, no context_id) (#1409)", async () => {
    setWorkspace("admin");
    armInstall();
    mockGetContexts.mockResolvedValue({
      contexts: [{ id: "ctx-1", name: "existing", display_name: "Existing" }],
      total: 1,
    });

    render(<ConnectorsPage />);

    // Switch away from the existing-connect default to the create-new field.
    fireEvent.click(
      await screen.findByRole("button", { name: "contextModeNew" }),
    );
    fireEvent.change(await screen.findByLabelText("contextName"), {
      target: { value: "brand-new-ctx" },
    });
    fireEvent.click(screen.getByRole("button", { name: "createConnector" }));

    await waitFor(() =>
      expect(mockCreateConnector).toHaveBeenCalledWith(
        expect.objectContaining({ auto_create_context_name: "brand-new-ctx" }),
      ),
    );
    const arg = mockCreateConnector.mock.calls[0][0];
    expect(arg).not.toHaveProperty("context_id");
  });

  it("defaults the write-target toggle to connect-existing when contexts exist (#1409)", async () => {
    setWorkspace("admin");
    armInstall();
    mockGetContexts.mockResolvedValue({
      contexts: [{ id: "ctx-1", name: "existing", display_name: "Existing" }],
      total: 1,
    });

    render(<ConnectorsPage />);

    // Existing-connect is the pressed default…
    const existing = await screen.findByRole("button", {
      name: "contextModeExisting",
    });
    expect(existing).toHaveAttribute("aria-pressed", "true");
    // …so the create-new name field is not rendered until the user switches.
    expect(screen.queryByLabelText("contextName")).not.toBeInTheDocument();
  });

  it("keeps the legacy create-new flow when the workspace has no contexts (#1409)", async () => {
    setWorkspace("admin");
    armInstall();
    // beforeEach default: mockGetContexts → { contexts: [], total: 0 }.

    render(<ConnectorsPage />);

    // Zero contexts → no mode toggle; the original single name field is the
    // only write-target control, so the first-connector flow is unchanged.
    await screen.findByRole("button", { name: "createConnector" });
    expect(
      screen.queryByRole("button", { name: "contextModeExisting" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "contextModeNew" }),
    ).not.toBeInTheDocument();

    fireEvent.change(await screen.findByLabelText("contextName"), {
      target: { value: "slack-first-ctx" },
    });
    fireEvent.click(screen.getByRole("button", { name: "createConnector" }));

    await waitFor(() =>
      expect(mockCreateConnector).toHaveBeenCalledWith(
        expect.objectContaining({
          auto_create_context_name: "slack-first-ctx",
        }),
      ),
    );
    const arg = mockCreateConnector.mock.calls[0][0];
    expect(arg).not.toHaveProperty("context_id");
  });

  it("surfaces a create error inside the dialog instead of silently closing (#1409)", async () => {
    setWorkspace("admin");
    armInstall();
    mockGetContexts.mockResolvedValue({
      contexts: [{ id: "ctx-1", name: "existing", display_name: "Existing" }],
      total: 1,
    });
    mockCreateConnector.mockRejectedValue(
      new Error("Context 'existing' already exists in this workspace"),
    );

    render(<ConnectorsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "createConnector" }),
    );

    // The 4xx is shown in-dialog (the old AlertDialogAction auto-close
    // swallowed it — #1409 "silent no-op").
    expect(
      await screen.findByText(
        "Context 'existing' already exists in this workspace",
      ),
    ).toBeInTheDocument();
    // The dialog stays open on failure.
    expect(screen.getByText("createTitle")).toBeInTheDocument();
  });
});
