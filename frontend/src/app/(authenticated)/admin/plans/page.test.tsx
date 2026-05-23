/**
 * Tests for the Plan Tiers tab on the admin plans page (Issue #664).
 *
 * Scope is the tiers tab only — workspaces / audit tabs are not exercised
 * here (covered by their own integration paths). We assert: the tab renders
 * 16 rows from ROW_DEFINITIONS, header columns reflect `display_name` from
 * the API (env-overridable), the info-card appears, ErrorBanner shows when
 * `getAdminPlanTiers` rejects, and a zero quota renders as "—" not "0".
 *
 * Skips the `useTabParam` URL plumbing by pinning the active tab via stub.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockGetAdminWorkspaces = vi.fn();
const mockGetAdminPlanAudit = vi.fn();
const mockGetAdminPlanTiers = vi.fn();
const mockGetWorkspaceQuotas = vi.fn();
const mockUpdateWorkspaceAddons = vi.fn();

vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    getAdminWorkspaces: (...args: unknown[]) => mockGetAdminWorkspaces(...args),
    getAdminPlanAudit: (...args: unknown[]) => mockGetAdminPlanAudit(...args),
    getAdminPlanTiers: (...args: unknown[]) => mockGetAdminPlanTiers(...args),
    getWorkspaceQuotas: (...args: unknown[]) => mockGetWorkspaceQuotas(...args),
    updateWorkspaceAddons: (...args: unknown[]) =>
      mockUpdateWorkspaceAddons(...args),
  };
});

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("next-intl", () => ({
  useTranslations: (namespace: string) => (key: string) =>
    namespace ? `${namespace}.${key}` : key,
  useLocale: () => "en",
}));

// `useTabParam` is mocked with a mutable `currentTab` so the same test
// file can exercise both the tiers tab (existing tests) and the
// workspaces tab (Issue #663 addon dialog tests). Each describe block
// sets `currentTab` in its `beforeEach`.
let currentTab: string = "tiers";
vi.mock("@/hooks/useTabParam", () => ({
  useTabParam: () => [currentTab, vi.fn()],
}));

vi.mock("@/components/common/PageContainer", () => ({
  PageContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/common/PageHeader", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));

import AdminPlansPage from "./page";

const FREE = {
  name: "free",
  display_name: "S",
  price_monthly: 0,
  max_contexts_per_workspace: 1,
  max_members_per_workspace: 1,
  max_resource_tokens: 0,
  memory_limit: 1000,
  mcp_calls_per_day: 1000,
  mcp_calls_per_week: 5000,
  rest_calls_per_day: 0,
  rest_calls_per_week: 0,
  public_calls_per_day: 0,
  public_calls_per_week: 0,
  bound_public_calls_per_minute: 0,
  analysis_runs_per_day: 0,
  storage_limit_bytes: 100 * 1024 * 1024,
  sleep_enabled_contexts_limit: 0,
  allows_shared_contexts: false,
  features: ["api_keys", "oauth"],
};

const BASIC = {
  ...FREE,
  name: "basic",
  display_name: "M",
  price_monthly: 10,
  max_contexts_per_workspace: 3,
  max_resource_tokens: 3,
  memory_limit: 10000,
  mcp_calls_per_day: 10000,
  rest_calls_per_day: 1000,
  storage_limit_bytes: 1024 * 1024 * 1024,
  features: ["api_keys", "oauth", "reranking"],
};

const PRO = {
  ...FREE,
  name: "pro",
  display_name: "L",
  price_monthly: 100,
  max_contexts_per_workspace: 20,
  max_members_per_workspace: 10,
  max_resource_tokens: 30,
  memory_limit: 100000,
  mcp_calls_per_day: 50000,
  rest_calls_per_day: 5000,
  public_calls_per_day: 1000,
  bound_public_calls_per_minute: 100,
  analysis_runs_per_day: 3,
  storage_limit_bytes: 10 * 1024 * 1024 * 1024,
  sleep_enabled_contexts_limit: 3,
  allows_shared_contexts: true,
  features: [
    "api_keys",
    "memory_analysis",
    "oauth",
    "public_contexts",
    "reranking",
    "shared_contexts",
    "team_invitations",
  ],
};

beforeEach(() => {
  currentTab = "tiers";
  mockGetAdminWorkspaces.mockResolvedValue([]);
  mockGetAdminPlanAudit.mockResolvedValue([]);
  mockGetAdminPlanTiers.mockResolvedValue([FREE, BASIC, PRO]);
  mockGetWorkspaceQuotas.mockReset();
  mockUpdateWorkspaceAddons.mockReset();
});

// ----------------------------------------------------------------------
// Workspaces tab fixtures (Issue #663 addon dialog coverage)
// ----------------------------------------------------------------------

const WORKSPACE_PRO_SUMMARY = {
  id: "ws-pro",
  name: "Pro Workspace",
  plan_name: "pro",
  owner_user_id: "user-1",
  owner_name: "Alice",
  owner_email: "alice@example.com",
  total_memories: 50_000,
  memory_limit: 100_000,
  mcp_calls_per_day: 50_000,
  mcp_calls_per_week: 250_000,
};

const QUOTA_DETAIL_PRO = {
  workspace_id: "ws-pro",
  workspace_name: "Pro Workspace",
  plan_name: "pro",
  base: {
    memory_limit: 100_000,
    mcp_calls_per_day: 50_000,
    max_contexts: 20,
    max_members: 10,
    analysis_runs_per_day: 3,
    rest_calls_per_day: 5_000,
    public_calls_per_day: 1_000,
    storage_bytes_limit: 10 * 1024 ** 3,
    sleep_enabled_contexts_limit: 3,
    max_resource_tokens: 30,
  },
  addon: {
    memory_bonus: 10_000,
    mcp_quota_bonus: 0,
    rest_quota_bonus: 0,
    public_quota_bonus: 0,
    member_bonus: 0,
    context_bonus: 0,
    analysis_bonus: 0,
    storage_bonus_mb: 0,
    sleep_contexts_bonus: 0,
  },
  effective: {
    memory_limit: 110_000,
    mcp_calls_per_day: 50_000,
    max_contexts: 20,
    max_members: 10,
    analysis_runs_per_day: 3,
    rest_calls_per_day: 5_000,
    public_calls_per_day: 1_000,
    storage_bytes_limit: 10 * 1024 ** 3,
    sleep_enabled_contexts_limit: 3,
    max_resource_tokens: 30,
  },
  usage: { memories: 50_000, contexts: 5, members: 3 },
  spend_cap: null,
};

describe("AdminPlansPage — tiers tab", () => {
  it("renders 16 ROW_DEFINITIONS rows once tiers load", async () => {
    render(<AdminPlansPage />);

    // Wait for one of the well-known row labels to appear (i18n stub
    // renders the key path verbatim — `admin.plans.tiersTable.memories`).
    await screen.findByText("admin.plans.tiersTable.memories");

    // Sanity: every ROW_DEFINITIONS key is rendered as a row label.
    const expectedRowKeys = [
      "contextsPerWorkspace",
      "memories",
      "mcpCallsPerDay",
      "analysisRuns",
      "reranking",
      "mcpAppCredentials",
      "storage",
      "maxMembers",
      "maxResourceTokens",
      "restCallsPerDay",
      "publicCallsPerDay",
      "boundPublicPerMinute",
      "sleepContextsLimit",
      "sharedContexts",
      "publicContexts",
      "memoryAnalysis",
    ];
    for (const key of expectedRowKeys) {
      expect(
        screen.getByText(`admin.plans.tiersTable.${key}`),
      ).toBeInTheDocument();
    }

    // memoryAgent was a vapor feature — the row must NOT render.
    expect(
      screen.queryByText("admin.plans.tiersTable.memoryAgent"),
    ).not.toBeInTheDocument();
  });

  it("uses tier display_name from API as column headers", async () => {
    render(<AdminPlansPage />);
    await screen.findByText("admin.plans.tiersTable.memories");

    // display_name comes from API (env-overridable); not hardcoded i18n.
    expect(screen.getByRole("columnheader", { name: "S" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "M" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "L" })).toBeInTheDocument();
  });

  it("renders zero-quota cells as em-dash, non-zero with locale grouping", async () => {
    render(<AdminPlansPage />);
    await screen.findByText("admin.plans.tiersTable.memories");

    // Locale grouping applies to ≥1000. Multiple rows hit each value
    // (memory_limit and mcp_calls_per_day both = 1000 on FREE), so use
    // getAllByText and assert presence rather than uniqueness.
    expect(screen.getAllByText("1,000").length).toBeGreaterThan(0);
    expect(screen.getAllByText("50,000").length).toBeGreaterThan(0);
    // Several rows have 0 on FREE/BASIC → em-dash.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("renders storage as GiB/MiB units based on byte magnitude", async () => {
    render(<AdminPlansPage />);
    await screen.findByText("admin.plans.tiersTable.memories");

    // Unit suffixes are unique to the storage row.
    expect(screen.getByText("100 MiB")).toBeInTheDocument();
    expect(screen.getByText("1 GiB")).toBeInTheDocument();
    expect(screen.getByText("10 GiB")).toBeInTheDocument();
  });

  it("locks the pivot-correction: mcp_calls_per_day, not legacy daily_api_limit", async () => {
    render(<AdminPlansPage />);
    await screen.findByText("admin.plans.tiersTable.memories");

    // Legacy daily_api_limit was 100/2000/10000 (mis-displayed pre-#664).
    // The new row must show actual mcp_calls_per_day 1000/10000/50000.
    expect(screen.getAllByText("1,000").length).toBeGreaterThan(0); // FREE
    expect(screen.getAllByText("10,000").length).toBeGreaterThan(0); // BASIC
    expect(screen.getAllByText("50,000").length).toBeGreaterThan(0); // PRO
    // The legacy BASIC daily_api_limit was 2000 — uniquely identifiable
    // (no other field/tier in our fixtures lands on 2000). If it appears,
    // the row regressed to legacy ``daily_api_limit`` from the rename.
    expect(screen.queryByText("2,000")).not.toBeInTheDocument();
  });

  it("renders the info-card with addon + zero-floor + env-override copy", async () => {
    render(<AdminPlansPage />);
    await screen.findByText("admin.plans.tiersTable.infoCard.title");

    expect(
      screen.getByText("admin.plans.tiersTable.infoCard.envOverrideBody"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("admin.plans.tiersTable.infoCard.addonBody"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("admin.plans.tiersTable.infoCard.zeroFloorBody"),
    ).toBeInTheDocument();
  });

  it("renders ErrorBanner when getAdminPlanTiers rejects without blocking other data", async () => {
    mockGetAdminPlanTiers.mockRejectedValueOnce(new Error("network"));

    render(<AdminPlansPage />);

    // Both the shadcn Alert info-card and ErrorBanner expose role=alert,
    // so identify the banner by its localized loadError copy directly
    // instead of role-narrowing.
    await screen.findByText("admin.plans.tiersTable.loadError");

    // Workspaces / audit fetches still ran (allSettled isolation).
    expect(mockGetAdminWorkspaces).toHaveBeenCalled();
    expect(mockGetAdminPlanAudit).toHaveBeenCalled();
  });
});

describe("AdminPlansPage — workspaces tab addon dialog (Issue #663)", () => {
  beforeEach(() => {
    currentTab = "workspaces";
    mockGetAdminWorkspaces.mockResolvedValue([WORKSPACE_PRO_SUMMARY]);
    mockGetWorkspaceQuotas.mockResolvedValue(QUOTA_DETAIL_PRO);
    mockUpdateWorkspaceAddons.mockResolvedValue(undefined);
  });

  // Opens the edit-addons dialog. Steps: wait for the workspace row, click
  // it to load quota detail, then click the "Edit Addons" button. Returns
  // once the dialog title is on screen so the caller can assert the input
  // tree directly.
  async function openAddonDialog(): Promise<void> {
    render(<AdminPlansPage />);
    const workspaceCell = await screen.findByText("Pro Workspace");
    fireEvent.click(workspaceCell);
    const editButton = await screen.findByText(
      "admin.plans.workspacesTable.editAddons",
    );
    fireEvent.click(editButton);
    await screen.findByText("admin.plans.addonDialog.title");
  }

  it("renders all 9 addon inputs with values pre-populated from quota detail", async () => {
    await openAddonDialog();

    // Every ADDON_TYPES entry has a stable input id of "addon-<key>".
    // The 9 keys in render order are memory / mcp / rest / public /
    // members / contexts / analysis / storage / sleep.
    const expectedIds = [
      "addon-memory",
      "addon-mcp",
      "addon-rest",
      "addon-public",
      "addon-members",
      "addon-contexts",
      "addon-analysis",
      "addon-storage",
      "addon-sleep",
    ];
    for (const id of expectedIds) {
      const input = document.getElementById(id) as HTMLInputElement | null;
      expect(input).not.toBeNull();
      expect(input!.tagName).toBe("INPUT");
    }
    // The memory addon is pre-populated from the cached bonus (#665
    // snapshot semantics): the dialog opens with the current cache value.
    const memoryInput = document.getElementById(
      "addon-memory",
    ) as HTMLInputElement;
    expect(memoryInput.value).toBe("10000");
  });

  it("submits all 9 fields to updateWorkspaceAddons on save", async () => {
    await openAddonDialog();

    // Mutate one input so the request body is verifiable against a
    // non-default value (memory_bonus 10000 → 20000, the next step).
    const memoryInput = document.getElementById(
      "addon-memory",
    ) as HTMLInputElement;
    fireEvent.change(memoryInput, { target: { value: "20000" } });

    const saveButton = screen.getByText("admin.plans.addonDialog.save");
    fireEvent.click(saveButton);

    // The PUT body must include all 9 addon_* fields so the backend's
    // no-touch contract (#665 review-fix #2) treats the request as
    // "send absolute values, no implicit deletes". The dialog is a
    // full-form submission, not a delta.
    expect(mockUpdateWorkspaceAddons).toHaveBeenCalledTimes(1);
    const [workspaceId, body] = mockUpdateWorkspaceAddons.mock.calls[0];
    expect(workspaceId).toBe("ws-pro");
    expect(body).toEqual({
      addon_memory_bonus: 20_000,
      addon_mcp_quota_bonus: 0,
      addon_rest_quota_bonus: 0,
      addon_public_quota_bonus: 0,
      addon_member_bonus: 0,
      addon_context_bonus: 0,
      addon_analysis_bonus: 0,
      addon_storage_bonus_mb: 0,
      addon_sleep_contexts_bonus: 0,
    });
  });

  it("renders the PRO-only inline note exactly once (on the sleep addon)", async () => {
    await openAddonDialog();

    // Only the sleep addon has `proOnly: true` in ADDON_TYPES, so the
    // localized "(PRO only)" hint must appear exactly once. If a future
    // refactor accidentally flips the flag on another addon (or drops
    // it from sleep), this assertion catches it.
    expect(
      screen.getAllByText("admin.plans.addonDialog.proOnlyInline"),
    ).toHaveLength(1);
  });

  it("renders the max_resource_tokens read-only row in the expanded panel", async () => {
    render(<AdminPlansPage />);
    const workspaceCell = await screen.findByText("Pro Workspace");
    fireEvent.click(workspaceCell);

    // The read-only row label uses the i18n key path `quota.maxResourceTokens`.
    // It exists because READ_ONLY_QUOTAS is rendered after ADDON_TYPES;
    // there is no corresponding addon dialog input for this dimension.
    await screen.findByText("admin.plans.quota.maxResourceTokens");
    // And the addon-bearing rows from ADDON_TYPES are present (sanity).
    expect(screen.getByText("admin.plans.quota.storage")).toBeInTheDocument();
    expect(screen.getByText("admin.plans.quota.sleep")).toBeInTheDocument();
  });
});
