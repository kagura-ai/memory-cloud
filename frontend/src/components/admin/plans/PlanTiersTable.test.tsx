/**
 * PlanTiersTable (#664; moved out of the Quotas page into the Users page's
 * Plan tiers tab in #2765). Asserts the row definitions, the API-driven
 * column headers, the value formatting, the info card and the error banner.
 */

import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PlanTiersTable } from "./PlanTiersTable";
import { BASIC, FREE, PRO, PROMAX } from "./plans.fixtures";

const mockGetAdminPlanTiers = vi.fn();
vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    getAdminPlanTiers: (...args: unknown[]) => mockGetAdminPlanTiers(...args),
  };
});

vi.mock("next-intl", () => ({
  useTranslations: (namespace: string) => (key: string) =>
    namespace ? `${namespace}.${key}` : key,
  useLocale: () => "en",
}));

beforeEach(() => {
  mockGetAdminPlanTiers.mockReset();
  mockGetAdminPlanTiers.mockResolvedValue([FREE, BASIC, PRO, PROMAX]);
});

/** Scope queries to one tiers-table row by its i18n label key. */
const tierRow = (key: string) =>
  within(
    screen
      .getByText(`admin.plans.tiersTable.${key}`)
      .closest("tr") as HTMLElement,
  );

describe("PlanTiersTable", () => {
  it("renders 20 ROW_DEFINITIONS rows once tiers load", async () => {
    render(<PlanTiersTable />);

    // Wait for one of the well-known row labels to appear (i18n stub
    // renders the key path verbatim — `admin.plans.tiersTable.memories`).
    await screen.findByText("admin.plans.tiersTable.memories");

    // Sanity: every ROW_DEFINITIONS key is rendered as a row label.
    const expectedRowKeys = [
      "contextsPerWorkspace",
      "memories",
      "memoriesPerDay", // Issue #1549
      "mcpCallsPerDay",
      "analysisRuns",
      "reranking",
      "mcpAppCredentials",
      "storage",
      "maxMembers",
      "ownedWorkspaces",
      "maxResourceTokens",
      "maxConnectors",
      "restCallsPerDay",
      "publicCallsPerDay",
      "boundPublicPerMinute",
      "sleepContextsLimit",
      "sharedContexts",
      "publicContexts",
      "resources",
      "connectors",
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

    // #1550: owned-workspace cap per tier (1 + grant) renders 1 / 1 / 3 / 20.
    const ownedRow = screen
      .getByText("admin.plans.tiersTable.ownedWorkspaces")
      .closest("tr") as HTMLElement;
    const ownedCells = within(ownedRow)
      .getAllByRole("cell")
      .slice(1)
      .map((c) => c.textContent);
    expect(ownedCells).toEqual(["1", "1", "3", "20"]);
  });

  it("uses tier display_name from API as column headers", async () => {
    render(<PlanTiersTable />);
    await screen.findByText("admin.plans.tiersTable.memories");

    // display_name comes from API (env-overridable); not hardcoded i18n.
    expect(screen.getByRole("columnheader", { name: "S" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "M" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "L" })).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "XL" }),
    ).toBeInTheDocument();
  });

  it("renders zero-quota cells as em-dash, non-zero with locale grouping", async () => {
    render(<PlanTiersTable />);
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
    render(<PlanTiersTable />);
    await screen.findByText("admin.plans.tiersTable.memories");

    // Unit suffixes are unique to the storage row.
    expect(screen.getByText("100 MiB")).toBeInTheDocument();
    expect(screen.getByText("1 GiB")).toBeInTheDocument();
    expect(screen.getByText("10 GiB")).toBeInTheDocument();
  });

  it("locks the pivot-correction: mcp_calls_per_day, not legacy daily_api_limit", async () => {
    render(<PlanTiersTable />);
    await screen.findByText("admin.plans.tiersTable.memories");

    // Legacy daily_api_limit was 100/2000/10000 (mis-displayed pre-#664).
    // The MCP row must show actual mcp_calls_per_day 1000/10000/50000.
    const mcpRow = tierRow("mcpCallsPerDay");
    expect(mcpRow.getByText("1,000")).toBeInTheDocument(); // FREE
    expect(mcpRow.getByText("10,000")).toBeInTheDocument(); // BASIC
    expect(mcpRow.getByText("50,000")).toBeInTheDocument(); // PRO
    // The legacy BASIC daily_api_limit was 2000. Scoped to THIS row: the
    // memories/day row legitimately shows 2,000 for PRO (#1549). If it shows
    // up here, the row regressed to legacy ``daily_api_limit``.
    expect(mcpRow.queryByText("2,000")).not.toBeInTheDocument();
  });

  it("renders the memories/day row (#1549) with the real tier values", async () => {
    render(<PlanTiersTable />);
    await screen.findByText("admin.plans.tiersTable.memoriesPerDay");

    const row = tierRow("memoriesPerDay");
    for (const cell of ["50", "300", "2,000", "10,000"]) {
      expect(row.getByText(cell)).toBeInTheDocument();
    }
  });

  it("renders — for a tier payload that predates memories_per_day (rolling deploy)", async () => {
    const legacyFree = { ...FREE } as Partial<typeof FREE>;
    delete legacyFree.memories_per_day;
    mockGetAdminPlanTiers.mockResolvedValue([legacyFree, BASIC, PRO, PROMAX]);
    render(<PlanTiersTable />);
    await screen.findByText("admin.plans.tiersTable.memoriesPerDay");

    const row = tierRow("memoriesPerDay");
    expect(row.getByText("—")).toBeInTheDocument(); // FREE: field missing
    expect(row.getByText("300")).toBeInTheDocument(); // the rest still render
  });

  it("renders the info-card with addon + zero-floor + env-override copy", async () => {
    render(<PlanTiersTable />);
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

  it("renders ErrorBanner when getAdminPlanTiers rejects", async () => {
    mockGetAdminPlanTiers.mockRejectedValueOnce(new Error("network"));

    render(<PlanTiersTable />);

    // Both the shadcn Alert info-card and ErrorBanner expose role=alert,
    // so identify the banner by its localized loadError copy directly
    // instead of role-narrowing.
    await screen.findByText("admin.plans.tiersTable.loadError");

    // The error is the panel's only channel: no table, no empty state.
    expect(
      screen.queryByText("admin.plans.tiersTable.emptyTitle"),
    ).not.toBeInTheDocument();
  });
});
