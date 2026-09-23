/**
 * Tests for the per-tier feature matrix (#1138).
 *
 * Covers: rows render from the API; numeric 0 → ✗; booleans → ✓/✗; locale
 * number + GiB/MiB storage formatting; current-tier highlight; and the hard
 * requirement that NO price is rendered (pricing lives on the payment side).
 */

import { act, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect, useState } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { PlanFeatureMatrix } from "./PlanFeatureMatrix";

const stableTranslator = (key: string) => key;
vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableTranslator,
}));
vi.mock("@/i18n", () => ({ useLocale: () => ({ locale: "en" }) }));
// Keep the real PLAN_TIER_ORDER; echo a canonical tier as its label (#1645:
// through planLabelForTier, which the table now calls for every column).
vi.mock("@/lib/utils/planLabel", async () => {
  const actual = await vi.importActual<typeof import("@/lib/utils/planLabel")>(
    "@/lib/utils/planLabel",
  );
  return {
    ...actual,
    planLabelFromEnv: (tier: string) => tier,
    planLabelForTier: (name: string, displayName: string | undefined) =>
      actual.isPlanTier(name) ? name : (displayName ?? name),
  };
});

// #1645: the table reads the shared matrix cache (usePlanTierMatrixState)
// instead of fetching on its own. The stand-in serves that hook's contract
// from `mockGetMatrix`, so each case still sets its payload the same way.
const mockGetMatrix = vi.fn();
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrixState: () => {
    const [state, setState] = useState<{
      tiers: unknown[] | null;
      failed: boolean;
    }>({ tiers: null, failed: false });
    useEffect(() => {
      Promise.resolve(mockGetMatrix()).then(
        (tiers) => setState({ tiers, failed: false }),
        () => setState({ tiers: null, failed: true }),
      );
    }, []);
    return state;
  },
}));

// #1654: the deployment flags the table consults. Default: a deployment that
// provides both flag-bearing features, i.e. the table exactly as the matrix
// serves it. `null` = /system/info still in flight.
let mockFeatures: Record<string, boolean> | null = {
  reranking: true,
  managed_llm: true,
};
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
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
    managed_llm: false,
    secret_store: true,
    shared_contexts: false,
    team_invitations: false,
    resources: false,
    connectors: false,
    public_contexts: false,
  },
  {
    // #1551: the matrix is the CREATION view — the API zeroes the
    // resource-token / connector / public rows below XL even though the tier
    // keeps serve-only caps for objects that already exist.
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
    max_resource_tokens: 0,
    max_connectors: 0,
    analysis_runs_per_day: 0,
    sleep_enabled_contexts_limit: 0,
    reranking: true,
    managed_embeddings: true,
    managed_llm: false,
    secret_store: true,
    shared_contexts: false,
    team_invitations: false,
    resources: false,
    connectors: false,
    public_contexts: false,
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
    public_calls_per_day: 0,
    max_resource_tokens: 0,
    max_connectors: 0,
    analysis_runs_per_day: 3,
    sleep_enabled_contexts_limit: 3,
    reranking: true,
    managed_embeddings: true,
    managed_llm: true,
    secret_store: true,
    shared_contexts: true,
    team_invitations: true,
    resources: false,
    connectors: false,
    public_contexts: false,
  },
  {
    // #1548: XL — every pro capability, higher limits. Values mirror the
    // backend PLAN_PROMAX registry (config/plan_tiers.py); #1551: the only
    // tier that may create resources / connectors / public contexts.
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
    managed_llm: true,
    secret_store: true,
    shared_contexts: true,
    team_invitations: true,
    resources: true,
    connectors: true,
    public_contexts: true,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockGetMatrix.mockResolvedValue(TIERS);
  mockFeatures = { reranking: true, managed_llm: true };
});

const rowOf = (label: string) =>
  within(screen.getByText(label).closest("tr") as HTMLElement);

describe("PlanFeatureMatrix (#1138)", () => {
  it("renders numeric limits with ✗ for zero", async () => {
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_connectors");

    // #1551: connector seats are a creation cap — ✗ everywhere below XL.
    const seats = rowOf("planMatrix.row_connectorSeats");
    expect(seats.getByText("50")).toBeInTheDocument(); // promax
    expect(seats.getAllByText("✗").length).toBe(3); // free / basic / pro
    const tokens = rowOf("planMatrix.row_resourceTokens");
    expect(tokens.getByText("150")).toBeInTheDocument(); // promax
    expect(tokens.getAllByText("✗").length).toBe(3);

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

    // #1569: managed_llm rides with memory_analysis (Pro-or-better).
    const managedLlm = rowOf("planMatrix.row_managedLlm");
    expect(managedLlm.getAllByText("✓").length).toBe(2);
    expect(managedLlm.getAllByText("✗").length).toBe(2);

    // secret_store (Volt) is included on every tier.
    const secrets = rowOf("planMatrix.row_secretStore");
    expect(secrets.getAllByText("✓").length).toBe(4);
    expect(secrets.queryByText("✗")).toBeNull();
  });

  it("marks resources / connectors / public features as XL-only (#1551)", async () => {
    render(<PlanFeatureMatrix currentTier="pro" />);
    await screen.findByText("planMatrix.row_resources");

    for (const key of [
      "planMatrix.row_resources",
      "planMatrix.row_connectors",
      "planMatrix.row_publicFeatures",
    ]) {
      const row = rowOf(key);
      expect(row.getAllByText("✓").length).toBe(1); // promax only
      expect(row.getAllByText("✗").length).toBe(3); // free / basic / pro
    }
    // Shared contexts stay on L — the public re-map does not drag them along.
    expect(
      rowOf("planMatrix.row_sharedContexts").getAllByText("✓").length,
    ).toBe(2);
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
      "planMatrix.row_connectorSeats",
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

describe("PlanFeatureMatrix and deployment flags (#1654)", () => {
  const rowLabels = () =>
    screen
      .getAllByRole("row")
      .map((tr) => tr.querySelector("td")?.textContent ?? "")
      .filter(Boolean);

  it("reranking off on this deployment: no tier shows it as a benefit", async () => {
    mockFeatures = { reranking: false, managed_llm: true };
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_connectors");

    expect(screen.queryByText("planMatrix.row_reranking")).toBeNull();
    // Only that row: the rest of the table is the matrix as served.
    expect(rowLabels()).not.toContain("planMatrix.row_reranking");
    expect(rowOf("planMatrix.row_managedLlm").getAllByText("✓").length).toBe(2);
  });

  it("reranking on: the table is unchanged", async () => {
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_reranking");

    const reranking = rowOf("planMatrix.row_reranking");
    expect(reranking.getAllByText("✓").length).toBe(3);
    expect(reranking.getAllByText("✗").length).toBe(1);
    expect(rowLabels()).toHaveLength(22); // every row the matrix defines
  });

  it("an older backend with no reranking flag keeps the row (#1580 polarity)", async () => {
    mockFeatures = { managed_llm: true };
    render(<PlanFeatureMatrix currentTier="basic" />);

    expect(
      await screen.findByText("planMatrix.row_reranking"),
    ).toBeInTheDocument();
  });

  it("/system/info pending: the table waits instead of listing a row it may withdraw", async () => {
    mockFeatures = null;
    const { rerender } = render(<PlanFeatureMatrix currentTier="basic" />);
    await waitFor(() => expect(mockGetMatrix).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    // Matrix loaded, flags not: no row at all — never a ✓ for reranking
    // that a moment later disappears.
    expect(screen.queryByText("planMatrix.row_contexts")).toBeNull();
    expect(screen.queryByText("planMatrix.row_reranking")).toBeNull();

    mockFeatures = { reranking: false, managed_llm: true };
    rerender(<PlanFeatureMatrix currentTier="basic" />);
    expect(
      await screen.findByText("planMatrix.row_contexts"),
    ).toBeInTheDocument();
    expect(screen.queryByText("planMatrix.row_reranking")).toBeNull();
  });

  it("is generic: the managed LLM row follows its own default-off flag", async () => {
    // managed_llm is on /system/info as "the deployment has a managed LLM
    // provider"; without one, no tier gets analysis on it.
    mockFeatures = { reranking: true, managed_llm: false };
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_reranking");

    expect(screen.queryByText("planMatrix.row_managedLlm")).toBeNull();
    expect(rowLabels()).toHaveLength(21);
  });

  it("a failed /system/info ({}) hides default-off rows and keeps the default-on reranker", async () => {
    mockFeatures = {};
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_reranking");

    expect(screen.queryByText("planMatrix.row_managedLlm")).toBeNull();
  });
});

// #1645: against the REAL shared cache — a fresh module graph per case, the
// API call the only stand-in.
describe("PlanFeatureMatrix on the shared matrix cache (#1645)", () => {
  async function loadReal(getPlanTierMatrix: () => Promise<unknown>) {
    vi.resetModules();
    vi.doUnmock("@/hooks/usePlanFeatures");
    vi.doMock("@/lib/api/workspaces", () => ({ getPlanTierMatrix }));
    const hooks = await import("@/hooks/usePlanFeatures");
    const { PlanFeatureMatrix: Table } = await import("./PlanFeatureMatrix");
    return { hooks, Table };
  }

  it("reads the shared cache instead of issuing its own fetch", async () => {
    const getPlanTierMatrix = vi.fn().mockResolvedValue(TIERS);
    const { hooks, Table } = await loadReal(getPlanTierMatrix);
    // A gate elsewhere on the page (the Sidebar, say) reads the same matrix.
    function Gate() {
      return (
        <span>{hooks.usePlanTierMatrix() ? "gate:ready" : "gate:pending"}</span>
      );
    }

    render(
      <>
        <Gate />
        <Table currentTier="basic" />
      </>,
    );
    await screen.findByText("planMatrix.row_connectors");
    expect(screen.getByText("gate:ready")).toBeInTheDocument();
    expect(getPlanTierMatrix).toHaveBeenCalledTimes(1);
  });

  it("renders an error banner when the retried fetch fails", async () => {
    vi.useFakeTimers();
    try {
      const getPlanTierMatrix = vi
        .fn()
        .mockRejectedValue(new Error("upstream said no"));
      const { Table } = await loadReal(getPlanTierMatrix);

      render(<Table currentTier="basic" />);
      // It inherits the shared hook's three attempts (500 ms / 1000 ms back-off).
      await vi.advanceTimersByTimeAsync(2000);
      expect(getPlanTierMatrix).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(0);
      // A translated message, never the transport's raw text.
      expect(screen.getByText("loadError")).toBeInTheDocument();
      expect(screen.queryByText(/upstream said no/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
