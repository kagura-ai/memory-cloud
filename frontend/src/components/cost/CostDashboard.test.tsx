/**
 * #1861: the breakdown table shows workspace names and user emails (falling
 * back to the shortened id), and the user cell links to the admin user page
 * only when the page passes ``userHref``.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CostAggregationRow } from "@/lib/api";
import { CostDashboard } from "./CostDashboard";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));

vi.mock("next/link", () => ({
  default: ({
    children,
    href,
    ...rest
  }: {
    children: React.ReactNode;
    href: string;
  }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("recharts", () => {
  const PassThrough = ({ children }: { children?: React.ReactNode }) => (
    <div>{children}</div>
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
    Legend: Empty,
  };
});

function row(over: Partial<CostAggregationRow>): CostAggregationRow {
  return {
    period_start: "2026-09-06",
    workspace_id: "61b5cf6f-0000-4000-8000-000000000000",
    workspace_name: "Team Alpha",
    user_id: "local:admin",
    user_email: "admin@example.com",
    calls: 2,
    tokens_in: 10,
    tokens_out: 5,
    tokens_cached_in: 0,
    embedding_tokens: 0,
    cost_usd: 0.001,
    cost_usd_byok: 0,
    cost_breakdown_by_model: [],
    cost_breakdown_by_source: [],
    ...over,
  };
}

const rows = [
  row({}),
  row({
    user_id: "10471144829432093253",
    user_email: null,
    workspace_name: null,
  }),
];

describe("CostDashboard rows", () => {
  it("shows workspace names and user emails, linking users on the admin page", async () => {
    render(
      <CostDashboard
        title="t"
        description="d"
        fetchData={async () => ({ rows })}
        showWorkspaceColumn
        userHref={(id) => `/admin/users/${encodeURIComponent(id)}`}
      />,
    );
    expect(await screen.findByText("Team Alpha")).toBeInTheDocument();
    expect(screen.getByText("61b5cf6f")).toBeInTheDocument(); // name unresolved → short id
    const link = screen.getByRole("link", { name: "admin@example.com" });
    expect(link).toHaveAttribute("href", "/admin/users/local%3Aadmin");
    expect(screen.getByRole("link", { name: "uid:10471144" })).toHaveAttribute(
      "href",
      "/admin/users/10471144829432093253",
    );
  });

  it("keeps the user cell plain text on the workspace page", async () => {
    render(
      <CostDashboard
        title="t"
        description="d"
        fetchData={async () => ({ rows })}
        showWorkspaceColumn={false}
      />,
    );
    expect(await screen.findByText("admin@example.com")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "admin@example.com" }),
    ).toBeNull();
    expect(screen.queryByText("Team Alpha")).toBeNull(); // no workspace column
  });
});
