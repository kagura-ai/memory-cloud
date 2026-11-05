import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { UsageHistoryResponse, UsageBreakdownResponse } from "@/lib/api/usage";
import { UsageTrends } from "./UsageTrends";

const mocks = vi.hoisted(() => ({ history: vi.fn(), breakdown: vi.fn(), workspace: "ws-1" }));
const translate = (key: string) => key;
vi.mock("next-intl", () => ({ useTranslations: () => translate, useLocale: () => "en" }));
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ user: { timezone: "UTC" } }) }));
vi.mock("@/contexts/WorkspaceContext", () => ({ useWorkspace: () => ({ currentWorkspaceId: mocks.workspace }) }));
vi.mock("@/lib/api/workspaces", () => ({
  getWorkspaceUsageHistory: (...args: unknown[]) => mocks.history(...args),
  getWorkspaceUsageBreakdown: (...args: unknown[]) => mocks.breakdown(...args),
}));
vi.mock("recharts", () => {
  const Container = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>;
  const Empty = () => null;
  return { ResponsiveContainer: Container, LineChart: Container, Line: Empty, Tooltip: Empty, XAxis: Empty, YAxis: Empty, CartesianGrid: Empty };
});
const history: UsageHistoryResponse = { daily_stats: [], total_requests: 0, period_start: "", period_end: "" };
const breakdown = (endpoint: string): UsageBreakdownResponse => ({
  by_endpoint: [{ endpoint, count: 12, percentage: 100 }], total_requests: 12, period_days: 30,
});
beforeEach(() => {
  vi.clearAllMocks();
  mocks.workspace = "ws-1";
  mocks.history.mockResolvedValue(history);
  mocks.breakdown.mockResolvedValue(breakdown("/first"));
});
describe("UsageTrends", () => {
  it("loads the existing time ranges and displays both summary charts", async () => {
    render(<UsageTrends />);
    await screen.findByText("/first");
    expect(screen.getByText("usageTrend")).toBeVisible();
    expect(screen.getByText("usageByEndpoint")).toBeVisible();
    expect(mocks.history).toHaveBeenCalledExactlyOnceWith(7);
    expect(mocks.breakdown).toHaveBeenCalledExactlyOnceWith(30);
  });
  it("does not show a previous workspace response after switching workspaces", async () => {
    let finishOld!: (data: UsageBreakdownResponse) => void;
    mocks.breakdown.mockReturnValueOnce(new Promise<UsageBreakdownResponse>(resolve => { finishOld = resolve; }));
    const { rerender } = render(<UsageTrends />);
    mocks.workspace = "ws-2";
    mocks.breakdown.mockResolvedValueOnce(breakdown("/second"));
    rerender(<UsageTrends />);
    await screen.findByText("/second");
    await act(async () => finishOld(breakdown("/old-private-endpoint")));
    expect(screen.queryByText("/old-private-endpoint")).not.toBeInTheDocument();
    expect(screen.getByText("/second")).toBeVisible();
  });
  it("shows a local load error when the history request fails", async () => {
    mocks.history.mockRejectedValueOnce(new Error("History unavailable"));
    render(<UsageTrends />);
    expect(await screen.findByRole("alert")).toHaveTextContent("History unavailable");
    expect(screen.queryByText("/first")).not.toBeInTheDocument();
  });
});
