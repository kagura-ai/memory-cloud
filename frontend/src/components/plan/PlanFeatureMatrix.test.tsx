/**
 * Tests for the per-tier feature matrix (#1138).
 *
 * Covers: rows render from the API; numeric 0 → ✗; booleans → ✓/✗; locale
 * number + GiB/MiB storage formatting; current-tier highlight; and the hard
 * requirement that NO price is rendered (pricing lives on the payment side).
 */

import { render, screen, within } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { PlanFeatureMatrix } from "./PlanFeatureMatrix";

const stableTranslator = (key: string) => key;
vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableTranslator,
}));
vi.mock("@/i18n", () => ({ useLocale: () => ({ locale: "en" }) }));
// Keep the real PLAN_TIER_ORDER (drives TIER_KEYS); echo the tier as its label.
vi.mock("@/lib/utils/planLabel", async () => ({
  ...(await vi.importActual<typeof import("@/lib/utils/planLabel")>(
    "@/lib/utils/planLabel",
  )),
  planLabelFromEnv: (tier: string) => tier,
}));

const mockGetMatrix = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  getPlanTierMatrix: () => mockGetMatrix(),
}));

const TIERS = [
  {
    name: "free",
    display_name: "S",
    max_contexts: 1,
    max_members: 1,
    owned_workspaces: 1,
    memory_limit: 1000,
    memories_per_day: 50,
    storage_limit_bytes: 100 * 1024 * 1024,
    mcp_calls_per_day: 1000,
    rest_calls_per_day: 0,
    public_calls_per_day: 0,
    max_resource_tokens: 0,
    max_connectors: 0,
    analysis_runs_per_day: 0,
    sleep_enabled_contexts_limit: 0,
    reranking: false,
    managed_embeddings: false,
    secret_store: true,
    shared_contexts: false,
    team_invitations: false,
  },
  {
    name: "basic",
    display_name: "M",
    max_contexts: 3,
    max_members: 1,
    owned_workspaces: 1,
    memory_limit: 10000,
    memories_per_day: 300,
    storage_limit_bytes: 1024 ** 3,
    mcp_calls_per_day: 10000,
    rest_calls_per_day: 1000,
    public_calls_per_day: 0,
    max_resource_tokens: 3,
    max_connectors: 3,
    analysis_runs_per_day: 0,
    sleep_enabled_contexts_limit: 0,
    reranking: true,
    managed_embeddings: true,
    secret_store: true,
    shared_contexts: false,
    team_invitations: false,
  },
  {
    name: "pro",
    display_name: "L",
    max_contexts: 20,
    max_members: 10,
    owned_workspaces: 3,
    memory_limit: 100000,
    memories_per_day: 2000,
    storage_limit_bytes: 10 * 1024 ** 3,
    mcp_calls_per_day: 50000,
    rest_calls_per_day: 5000,
    public_calls_per_day: 1000,
    max_resource_tokens: 30,
    max_connectors: 10,
    analysis_runs_per_day: 3,
    sleep_enabled_contexts_limit: 3,
    reranking: true,
    managed_embeddings: true,
    secret_store: true,
    shared_contexts: true,
    team_invitations: true,
  },
  {
    // #1548: XL — every pro capability, higher limits. Values mirror the
    // backend PLAN_PROMAX registry (config/plan_tiers.py).
    name: "promax",
    display_name: "XL",
    max_contexts: 1000,
    max_members: 50,
    owned_workspaces: 20,
    memory_limit: 100000,
    memories_per_day: 10000,
    storage_limit_bytes: 50 * 1024 ** 3,
    mcp_calls_per_day: 250000,
    rest_calls_per_day: 25000,
    public_calls_per_day: 5000,
    max_resource_tokens: 150,
    max_connectors: 50,
    analysis_runs_per_day: 15,
    sleep_enabled_contexts_limit: 15,
    reranking: true,
    managed_embeddings: true,
    secret_store: true,
    shared_contexts: true,
    team_invitations: true,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockGetMatrix.mockResolvedValue(TIERS);
});

const rowOf = (label: string) =>
  within(screen.getByText(label).closest("tr") as HTMLElement);

describe("PlanFeatureMatrix (#1138)", () => {
  it("renders numeric limits with ✗ for zero", async () => {
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_connectors");

    const connectors = rowOf("planMatrix.row_connectors");
    expect(connectors.getByText("3")).toBeInTheDocument(); // basic
    expect(connectors.getByText("10")).toBeInTheDocument(); // pro
    expect(connectors.getByText("50")).toBeInTheDocument(); // promax
    expect(connectors.getAllByText("✗").length).toBe(1); // free = 0

    // Locale-grouped number + GiB storage. pro and promax share the memory
    // limit (promax is a superset on features, not every quota).
    expect(
      rowOf("planMatrix.row_memories").getAllByText("100,000").length,
    ).toBe(2);
    expect(
      rowOf("planMatrix.row_storage").getByText("100 MiB"),
    ).toBeInTheDocument();
    expect(
      rowOf("planMatrix.row_storage").getByText("10 GiB"),
    ).toBeInTheDocument();
    expect(
      rowOf("planMatrix.row_storage").getByText("50 GiB"),
    ).toBeInTheDocument();
  });

  it("renders the memories-per-day row (#1549) as a stable numeric row", async () => {
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_memoriesPerDay");

    // S 50 · M 300 · L 2,000 · XL 10,000 — locale-grouped like the other
    // numeric rows, no Beta badge, and sits right under the memory cap.
    const row = rowOf("planMatrix.row_memoriesPerDay");
    expect(row.getByText("50")).toBeInTheDocument();
    expect(row.getByText("300")).toBeInTheDocument();
    expect(row.getByText("2,000")).toBeInTheDocument();
    expect(row.getByText("10,000")).toBeInTheDocument();
    expect(row.queryByText("✗")).toBeNull();
    expect(row.queryByText("planMatrix.beta")).toBeNull();

    const labels = screen
      .getAllByRole("row")
      .map((tr) => tr.querySelector("td")?.textContent ?? "");
    expect(labels.indexOf("planMatrix.row_memoriesPerDay")).toBe(
      labels.indexOf("planMatrix.row_memories") + 1,
    );
  });

  it("renders ✗ (not a crash) for a tier payload that predates memories_per_day", async () => {
    // Rolling deploy: the frontend ships before the API; the field is absent.
    const legacyFree = { ...TIERS[0] } as Partial<(typeof TIERS)[number]>;
    delete legacyFree.memories_per_day;
    mockGetMatrix.mockResolvedValue([legacyFree, ...TIERS.slice(1)]);
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_memoriesPerDay");

    const row = rowOf("planMatrix.row_memoriesPerDay");
    expect(row.getAllByText("✗").length).toBe(1); // free: field missing
    expect(row.getByText("300")).toBeInTheDocument(); // the rest still render
    expect(row.getByText("10,000")).toBeInTheDocument();
  });

  it("renders the owned-workspaces row (1 / 1 / 3 / 20) next to members (#1550)", async () => {
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_ownedWorkspaces");

    const owned = rowOf("planMatrix.row_ownedWorkspaces");
    const cells = owned.getAllByRole("cell").map((c) => c.textContent);
    expect(cells).toEqual([
      "planMatrix.row_ownedWorkspaces",
      "1",
      "1",
      "3",
      "20",
    ]);
    expect(owned.queryByText("✗")).toBeNull(); // every tier owns at least one

    // Sits directly under the members row.
    const rows = screen.getAllByRole("row").map((r) => r.textContent ?? "");
    const membersIdx = rows.findIndex((r) =>
      r.startsWith("planMatrix.row_members"),
    );
    expect(rows[membersIdx + 1]).toMatch(/^planMatrix\.row_ownedWorkspaces/);
  });

  it("renders ✓/✗ for boolean capabilities", async () => {
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_reranking");

    const reranking = rowOf("planMatrix.row_reranking");
    expect(reranking.getAllByText("✓").length).toBe(3); // basic + pro + promax
    expect(reranking.getAllByText("✗").length).toBe(1); // free

    // team_invitations is Pro-or-better.
    const team = rowOf("planMatrix.row_teamInvitations");
    expect(team.getAllByText("✓").length).toBe(2);
    expect(team.getAllByText("✗").length).toBe(2);

    // secret_store (Volt) is included on every tier.
    const secrets = rowOf("planMatrix.row_secretStore");
    expect(secrets.getAllByText("✓").length).toBe(4);
    expect(secrets.queryByText("✗")).toBeNull();
  });

  it("labels every PLAN_TIER_ORDER tier via planLabelFromEnv, unknown tiers via display_name (#1548)", async () => {
    mockGetMatrix.mockResolvedValue([
      ...TIERS,
      { ...TIERS[3], name: "enterprise", display_name: "Enterprise" },
    ]);
    render(<PlanFeatureMatrix />);
    await screen.findByText("planMatrix.row_connectors");

    // promax is a known key → env-resolvable label (mock echoes the tier),
    // NOT the backend display_name.
    expect(
      screen.getByRole("columnheader", { name: "promax" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("XL")).toBeNull();
    // A tier the frontend doesn't know falls back to display_name.
    expect(
      screen.getByRole("columnheader", { name: "Enterprise" }),
    ).toBeInTheDocument();
  });

  it("highlights the current tier and never renders a price", async () => {
    render(<PlanFeatureMatrix currentTier="pro" />);
    await screen.findByText("planMatrix.row_connectors");

    expect(screen.getByText(/planMatrix\.current/)).toBeInTheDocument();
    // No price/currency leaks into the matrix.
    expect(document.body.textContent ?? "").not.toMatch(/\$|¥|price/i);
  });

  it("tags Connectors / Analysis / Sleep / Secrets rows as Beta, not the stable rows", async () => {
    render(<PlanFeatureMatrix currentTier="pro" />);
    await screen.findByText("planMatrix.row_connectors");

    for (const key of [
      "planMatrix.row_connectors",
      "planMatrix.row_analysisPerDay",
      "planMatrix.row_sleepContexts",
      "planMatrix.row_secretStore",
    ]) {
      expect(rowOf(key).getByText("planMatrix.beta")).toBeInTheDocument();
    }
    // A stable capability row carries no Beta badge.
    expect(
      rowOf("planMatrix.row_memories").queryByText("planMatrix.beta"),
    ).toBeNull();
  });
});
