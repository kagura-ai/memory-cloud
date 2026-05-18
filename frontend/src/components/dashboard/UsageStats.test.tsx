/**
 * Tests for UsageStats (Issue #571).
 *
 * Covers the gate1 scope:
 *   - Sleep-enabled contexts card is rendered iff usage.sleep_contexts !== null
 *   - Progress value clamps at 100% when used / limit > 1 (visible via the
 *     rendered progress aria-valuenow, since used/limit text is also shown
 *     verbatim and the clamping is a separate calculation)
 *   - "sleepContextsWithAddon" text only shown when limit > 0 && addon > 0;
 *     "sleepContextsTier" otherwise (defense-in-depth gate from #560 loop 8)
 *   - Memory / API Calls Today / API Calls This Week cards render numbers
 *   - Loading state, error state
 *
 * Recharts ResponsiveContainer needs DOM dimensions to render. Happy-dom
 * reports zero-sized parents, which would normally suppress chart children
 * silently. We mock the recharts surface to a pass-through so the trend
 * card itself renders (and its chart container is testable as a presence
 * check), without depending on layout.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { UsageStats } from "./UsageStats";
import type {
  PlanLimits,
  CurrentUsage,
  UsageCurrentResponse,
} from "@/lib/api/usage";

// ---------- Mocks ------------------------------------------------------------

const mockGetWorkspaceUsageCurrent = vi.fn();
const mockGetWorkspaceUsageHistory = vi.fn();
const mockGetWorkspaceUsageBreakdown = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  getWorkspaceUsageCurrent: (...a: unknown[]) =>
    mockGetWorkspaceUsageCurrent(...a),
  getWorkspaceUsageHistory: (...a: unknown[]) =>
    mockGetWorkspaceUsageHistory(...a),
  getWorkspaceUsageBreakdown: (...a: unknown[]) =>
    mockGetWorkspaceUsageBreakdown(...a),
}));

// Translator returns the key plus any `addon` var that this test file
// asserts on. Other interpolations fall through to the bare key — the
// tests query by stable key text rather than rendered numbers, so
// `count`/`percent` branches would never be exercised.
const stableT = (key: string, vars?: Record<string, unknown>) => {
  if (vars && "addon" in vars) return `${key}:+${vars.addon}`;
  return key;
};
vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableT,
  useLocale: () => "en",
}));

vi.mock("@/contexts/MemoryContextContext", () => ({
  useMemoryContext: () => ({ contextId: "ctx-1" }),
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-1", timezone: "UTC" } }),
}));

// QuotaWarning renders its own conditional UI based on quota; stub it out
// so the unit under test is the rest of UsageStats, not the warning banner.
vi.mock("@/components/common/QuotaWarning", () => ({
  QuotaWarning: () => null,
}));

// Recharts pass-through. ResponsiveContainer would otherwise depend on layout
// dimensions that happy-dom can't compute.
vi.mock("recharts", () => {
  const PassThrough = ({ children }: { children?: React.ReactNode }) => (
    <div data-testid="recharts-stub">{children}</div>
  );
  const Empty = () => null;
  return {
    LineChart: PassThrough,
    Line: Empty,
    ResponsiveContainer: PassThrough,
    Tooltip: Empty,
    XAxis: Empty,
    YAxis: Empty,
    CartesianGrid: Empty,
  };
});

// ---------- Helpers ----------------------------------------------------------

function makePlan(overrides: Partial<PlanLimits> = {}): PlanLimits {
  return {
    plan_name: "free",
    memory_limit: 1000,
    daily_total_limit: 100,
    weekly_total_limit: 500,
    mcp_calls_per_day: 50,
    mcp_calls_per_week: 250,
    rest_calls_per_day: 40,
    rest_calls_per_week: 200,
    public_calls_per_day: 10,
    public_calls_per_week: 50,
    ...overrides,
  };
}

function makeUsage(overrides: Partial<CurrentUsage> = {}): CurrentUsage {
  return {
    memory_count: 42,
    api_calls_today: 17,
    api_calls_this_week: 89,
    mcp_calls_today: 7,
    mcp_calls_this_week: 30,
    rest_calls_today: 8,
    rest_calls_this_week: 40,
    public_calls_today: 2,
    public_calls_this_week: 19,
    sleep_contexts: null,
    // Distinct from common sleep_contexts test ratios so getByText
    // matchers on "1 / 3" or "5 / 2" don't accidentally hit the
    // workspaces card too.
    workspaces: { used: 4, limit: 8, remaining: 4 },
    ...overrides,
  };
}

function makeCurrentResponse(
  overrides: Partial<UsageCurrentResponse> = {},
): UsageCurrentResponse {
  return {
    plan: makePlan(),
    usage: makeUsage(),
    memory_usage: {
      current: 42,
      limit: 1000,
      percentage: 4.2,
      is_warning: false,
      is_critical: false,
      is_exceeded: false,
    },
    daily_api_usage: {
      current: 17,
      limit: 100,
      percentage: 17,
      is_warning: false,
      is_critical: false,
      is_exceeded: false,
    },
    weekly_api_usage: {
      current: 89,
      limit: 500,
      percentage: 17.8,
      is_warning: false,
      is_critical: false,
      is_exceeded: false,
    },
    ...overrides,
  };
}

const HISTORY_OK = {
  daily_stats: [{ date: "2026-05-10", count: 5 }],
  total_requests: 5,
  period_start: "2026-05-10T00:00:00Z",
  period_end: "2026-05-17T00:00:00Z",
};

const BREAKDOWN_OK = {
  by_endpoint: [
    { endpoint: "/api/v1/memory/recall", count: 30, percentage: 60 },
    { endpoint: "/api/v1/memory/remember", count: 20, percentage: 40 },
  ],
  total_requests: 50,
  period_days: 30,
};

beforeEach(() => {
  vi.clearAllMocks();
  mockUseWorkspace.mockReturnValue({ currentWorkspaceId: "ws-1" });
  // History/Breakdown are stable across all tests in this file — only
  // the current-usage shape varies per case.
  mockGetWorkspaceUsageHistory.mockResolvedValue(HISTORY_OK);
  mockGetWorkspaceUsageBreakdown.mockResolvedValue(BREAKDOWN_OK);
});

// ---------- Sleep-enabled contexts card -------------------------------------

describe("UsageStats — sleep-enabled contexts card", () => {
  it("does NOT render the sleep_contexts card when usage.sleep_contexts is null", async () => {
    mockGetWorkspaceUsageCurrent.mockResolvedValue(
      makeCurrentResponse({ usage: makeUsage({ sleep_contexts: null }) }),
    );
    render(<UsageStats scope="workspace" />);

    // Wait for the dashboard body to land (memories card is always present).
    await screen.findByText("memories");
    expect(screen.queryByText("sleepEnabledContexts")).not.toBeInTheDocument();
  });

  it("renders the sleep_contexts card when sleep_contexts has values", async () => {
    mockGetWorkspaceUsageCurrent.mockResolvedValue(
      makeCurrentResponse({
        usage: makeUsage({
          sleep_contexts: { used: 1, limit: 3, addon_bonus: 0, remaining: 2 },
        }),
      }),
    );
    render(<UsageStats scope="workspace" />);

    await screen.findByText("sleepEnabledContexts");
    // "1 / 3" rendered. Use a function matcher because the text spans
    // multiple spans (number, slash, number).
    expect(
      screen.getByText((_content, node) => node?.textContent === "1 / 3"),
    ).toBeInTheDocument();
  });

  it("clamps the progress value at 100% when used / limit > 1", async () => {
    mockGetWorkspaceUsageCurrent.mockResolvedValue(
      makeCurrentResponse({
        usage: makeUsage({
          sleep_contexts: { used: 5, limit: 2, addon_bonus: 0, remaining: 0 },
        }),
      }),
    );
    render(<UsageStats scope="workspace" />);

    // The local shadcn Progress (src/components/ui/progress.tsx) renders
    // the percentage as `transform: translateX(-${100-pct}%)` on an
    // inner div with bg-brand-green-600 — there is no role="progressbar".
    // Without the clamp, used=5/limit=2 would compute 250%, leaving the
    // residual at -150 (an off-screen positive translate). The clamp
    // must drive the residual to 0 regardless of sign convention.
    const title = await screen.findByText("sleepEnabledContexts");
    const card = title.closest("div.rounded-xl");
    const indicator = card?.querySelector<HTMLElement>(
      "[class*='bg-brand-green-600']",
    );
    expect(indicator).toBeTruthy();
    const match = indicator!.style.transform.match(
      /translateX\((-?\d+(?:\.\d+)?)%\)/,
    );
    expect(match).not.toBeNull();
    // Math.abs normalizes the `Object.is(-0, +0) === false` quirk —
    // the assertion is that the clamp drove the residual to zero,
    // regardless of which sign convention the transform emits.
    expect(Math.abs(Number(match![1]))).toBe(0);
  });

  it("shows sleepContextsWithAddon ONLY when limit > 0 AND addon_bonus > 0", async () => {
    mockGetWorkspaceUsageCurrent.mockResolvedValue(
      makeCurrentResponse({
        usage: makeUsage({
          sleep_contexts: { used: 1, limit: 5, addon_bonus: 2, remaining: 4 },
        }),
      }),
    );
    render(<UsageStats scope="workspace" />);

    await screen.findByText("sleepContextsWithAddon:+2");
    expect(screen.queryByText("sleepContextsTier")).not.toBeInTheDocument();
  });

  it("shows sleepContextsTier when limit > 0 but addon_bonus === 0", async () => {
    mockGetWorkspaceUsageCurrent.mockResolvedValue(
      makeCurrentResponse({
        usage: makeUsage({
          sleep_contexts: { used: 1, limit: 5, addon_bonus: 0, remaining: 4 },
        }),
      }),
    );
    render(<UsageStats scope="workspace" />);

    await screen.findByText("sleepContextsTier");
    expect(
      screen.queryByText(/sleepContextsWithAddon/),
    ).not.toBeInTheDocument();
  });

  it("shows sleepContextsTier (NOT the addon text) when limit === 0 even if addon_bonus > 0", async () => {
    // Defense-in-depth gate from #560 loop 8: backend normalizes
    // addon_bonus to 0 for zero-base tiers, but the explicit
    // `limit > 0` guard means the card still falls back to the tier
    // text even if a future regression leaks a non-zero addon.
    mockGetWorkspaceUsageCurrent.mockResolvedValue(
      makeCurrentResponse({
        usage: makeUsage({
          sleep_contexts: { used: 0, limit: 0, addon_bonus: 2, remaining: 0 },
        }),
      }),
    );
    render(<UsageStats scope="workspace" />);

    await screen.findByText("sleepContextsTier");
    expect(
      screen.queryByText(/sleepContextsWithAddon/),
    ).not.toBeInTheDocument();
  });
});

// ---------- Memory / API Today / API This Week cards ------------------------

describe("UsageStats — primary usage cards", () => {
  it("renders Memory / API Today / API This Week cards with the right numbers", async () => {
    mockGetWorkspaceUsageCurrent.mockResolvedValue(makeCurrentResponse());
    render(<UsageStats scope="workspace" />);

    // Card titles
    await screen.findByText("memories");
    expect(screen.getByText("apiCallsToday")).toBeInTheDocument();
    expect(screen.getByText("apiCallsThisWeek")).toBeInTheDocument();

    // The values render as separate spans split by "/". Use textContent
    // matchers so we assert the combined "42 / 1000" form.
    expect(
      screen.getByText((_c, node) => node?.textContent === "42 / 1000"),
    ).toBeInTheDocument();
    expect(
      screen.getByText((_c, node) => node?.textContent === "17 / 100"),
    ).toBeInTheDocument();
    expect(
      screen.getByText((_c, node) => node?.textContent === "89 / 500"),
    ).toBeInTheDocument();
  });
});

// ---------- Loading and error states ----------------------------------------

describe("UsageStats — loading and error states", () => {
  it("renders loading skeletons while the first fetch is in flight", async () => {
    // Hold the current-usage fetch with a deferred promise so the loading
    // branch is observable, then resolve before the test exits — leaving an
    // unresolved `new Promise(() => {})` would retain worker references and
    // contribute to vmForks teardown timeouts (memory `1961cb46`).
    let resolveCurrent: (v: UsageCurrentResponse) => void = () => {};
    mockGetWorkspaceUsageCurrent.mockReturnValueOnce(
      new Promise<UsageCurrentResponse>((r) => {
        resolveCurrent = r;
      }),
    );

    render(<UsageStats scope="workspace" />);

    // Dashboard cards must NOT have rendered while the fetch is in flight.
    expect(screen.queryByText("memories")).not.toBeInTheDocument();
    expect(screen.queryByText("apiCallsToday")).not.toBeInTheDocument();
    // Skeleton block present (component uses animate-pulse).
    expect(document.querySelector("[class*='animate-pulse']")).not.toBeNull();

    // Flush so the worker can clean up.
    resolveCurrent(makeCurrentResponse());
    await screen.findByText("memories");
  });

  it("renders the error Alert when the current-usage fetch rejects", async () => {
    mockGetWorkspaceUsageCurrent.mockRejectedValue(new Error("boom"));
    render(<UsageStats scope="workspace" />);

    // Error message from the rejected promise lands in the Alert body.
    // The component prefers err.message over the fallback "failedToLoad" key.
    await screen.findByText("boom");
    // Dashboard cards must not render after error.
    expect(screen.queryByText("memories")).not.toBeInTheDocument();
  });
});
