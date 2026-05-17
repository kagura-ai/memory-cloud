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

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockGetAdminWorkspaces = vi.fn();
const mockGetAdminPlanAudit = vi.fn();
const mockGetAdminPlanTiers = vi.fn();

vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    getAdminWorkspaces: (...args: unknown[]) => mockGetAdminWorkspaces(...args),
    getAdminPlanAudit: (...args: unknown[]) => mockGetAdminPlanAudit(...args),
    getAdminPlanTiers: (...args: unknown[]) => mockGetAdminPlanTiers(...args),
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

// Pin tab to "tiers" so the tiers TabsContent renders.
vi.mock("@/hooks/useTabParam", () => ({
  useTabParam: () => ["tiers", vi.fn()],
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
  mockGetAdminWorkspaces.mockResolvedValue([]);
  mockGetAdminPlanAudit.mockResolvedValue([]);
  mockGetAdminPlanTiers.mockResolvedValue([FREE, BASIC, PRO]);
});

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
