/**
 * OverviewTabPanel (#3035): who gets the public endpoint statistics card.
 *
 * A workspace owner of a context with a live public MCP endpoint gets the
 * card (and its request); an admin or member gets neither, and still sees
 * what the existing "Public API Usage" card shows.
 * The never-delivered card (#3022) is mocked so no test sends a request;
 * its own suite is OverviewTabPanel.test.tsx.
 */

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import en from "@/messages/en.json";
import type { Context } from "@/lib/types/context";

const mockGetStats = vi.fn();
vi.mock("@/lib/api/resources", () => ({
  getPublicMcpUsageStats: (...a: unknown[]) => mockGetStats(...a),
}));

vi.mock("@/lib/api/workspaces", () => ({
  getContextUsageTimeline: vi.fn().mockResolvedValue({
    daily_usage: [],
    total_calls: 0,
  }),
  getContextUserActivity: vi.fn().mockResolvedValue({ users: [] }),
  getContextPublicAPIStats: vi.fn().mockResolvedValue({}),
}));

vi.mock("@/components/contexts/NeverDeliveredCard", () => ({
  NeverDeliveredCard: () => null,
}));

vi.mock("@/components/dashboard/RichMemoryOverview", () => ({
  RichMemoryOverview: () => null,
}));

vi.mock("@/components/dashboard/PublicAPIStats", () => ({
  PublicAPIStats: () => <div data-testid="public-api-stats" />,
}));

vi.mock("recharts", () => {
  const Pass = ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  );
  const Nothing = () => null;
  return {
    ResponsiveContainer: Pass,
    LineChart: Pass,
    Line: Nothing,
    XAxis: Nothing,
    YAxis: Nothing,
    Tooltip: Nothing,
    CartesianGrid: Nothing,
  };
});

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

let role = "owner";
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: { current_user_role: role },
    currentWorkspaceId: "ws-1",
  }),
}));

import { OverviewTabPanel } from "./OverviewTabPanel";

const CONTEXT = {
  id: "ctx-1",
  name: "catalog",
  is_public: true,
  public_mcp_endpoint_live: true,
  resource_id: "catalog",
} as unknown as Context;

function renderPanel(context: Context = CONTEXT) {
  return render(
    <NextIntlClientProvider
      locale="en"
      messages={en as Record<string, unknown>}
      timeZone="UTC"
    >
      <OverviewTabPanel contextId="ctx-1" context={context} />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  mockGetStats.mockReset();
  mockGetStats.mockReturnValue(new Promise(() => {}));
});

afterEach(() => {
  cleanup();
});

describe("OverviewTabPanel public endpoint card", () => {
  it("mounts the card for an owner of a context with a live endpoint", async () => {
    role = "owner";
    renderPanel();
    expect(
      await screen.findByText("Public MCP endpoint usage"),
    ).toBeInTheDocument();
    expect(mockGetStats).toHaveBeenCalledWith("catalog", 7);
  });

  it.each(["admin", "member", "viewer"])(
    "gives %s no card and no request, and still the Public API Usage card",
    async (r) => {
      role = r;
      renderPanel();
      expect(await screen.findByTestId("public-api-stats")).toBeInTheDocument();
      expect(
        screen.queryByText("Public MCP endpoint usage"),
      ).not.toBeInTheDocument();
      expect(mockGetStats).not.toHaveBeenCalled();
    },
  );

  it("gives an owner no card without a live endpoint", async () => {
    role = "owner";
    renderPanel({
      ...CONTEXT,
      public_mcp_endpoint_live: false,
    } as Context);
    await waitFor(() =>
      expect(screen.getByTestId("public-api-stats")).toBeInTheDocument(),
    );
    expect(
      screen.queryByText("Public MCP endpoint usage"),
    ).not.toBeInTheDocument();
    expect(mockGetStats).not.toHaveBeenCalled();
  });
});
