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
vi.mock("@/lib/utils/planLabel", () => ({
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
    memory_limit: 1000,
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
    shared_contexts: false,
    team_invitations: false,
  },
  {
    name: "basic",
    display_name: "M",
    max_contexts: 3,
    max_members: 1,
    memory_limit: 10000,
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
    shared_contexts: false,
    team_invitations: false,
  },
  {
    name: "pro",
    display_name: "L",
    max_contexts: 20,
    max_members: 10,
    memory_limit: 100000,
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
    expect(connectors.getAllByText("✗").length).toBe(1); // free = 0

    // Locale-grouped number + GiB storage.
    expect(
      rowOf("planMatrix.row_memories").getByText("100,000"),
    ).toBeInTheDocument();
    expect(
      rowOf("planMatrix.row_storage").getByText("100 MiB"),
    ).toBeInTheDocument();
    expect(
      rowOf("planMatrix.row_storage").getByText("10 GiB"),
    ).toBeInTheDocument();
  });

  it("renders ✓/✗ for boolean capabilities", async () => {
    render(<PlanFeatureMatrix currentTier="basic" />);
    await screen.findByText("planMatrix.row_reranking");

    const reranking = rowOf("planMatrix.row_reranking");
    expect(reranking.getAllByText("✓").length).toBe(2); // basic + pro
    expect(reranking.getAllByText("✗").length).toBe(1); // free

    // team_invitations is Pro-only.
    const team = rowOf("planMatrix.row_teamInvitations");
    expect(team.getAllByText("✓").length).toBe(1);
    expect(team.getAllByText("✗").length).toBe(2);
  });

  it("highlights the current tier and never renders a price", async () => {
    render(<PlanFeatureMatrix currentTier="pro" />);
    await screen.findByText("planMatrix.row_connectors");

    expect(screen.getByText(/planMatrix\.current/)).toBeInTheDocument();
    // No price/currency leaks into the matrix.
    expect(document.body.textContent ?? "").not.toMatch(/\$|¥|price/i);
  });
});
