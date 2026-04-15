/**
 * Tests for ResourceStatsStrip.
 *
 * Verifies:
 * - all four KpiCards render with expected values
 * - schema_version === null renders em-dash + "not registered" subtext
 * - schema_version > 0 renders "v<version>"
 * - Last Activity card carries an absolute-time `title` for hover tooltip
 *
 * The "Manage" deep-link that used to overlay the Tokens KpiCard was removed
 * in #326 polish — Tokens is now reachable via the in-page tab bar.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ResourceStatsStrip } from "./ResourceStatsStrip";
import type { ResourceListItem } from "@/lib/api/resources";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatRelativeTime: (iso: string) => `relative(${iso})`,
  formatDateTime: (iso: string) => `dt(${iso})`,
}));

const make = (overrides: Partial<ResourceListItem> = {}): ResourceListItem => ({
  resource_id: "ec_products",
  context_id: "550e8400-e29b-41d4-a716-446655440000",
  context_name: "ec-products",
  context_display_name: "EC Products",
  token_count: 2,
  memory_count: 47,
  current_schema_version: 3,
  created_at: "2026-03-01T00:00:00Z",
  updated_at: "2026-04-14T09:15:30Z",
  ...overrides,
});

describe("ResourceStatsStrip", () => {
  it("renders token/memory counts and schema version", () => {
    render(<ResourceStatsStrip resource={make()} />);
    expect(screen.getByText("2")).toBeInTheDocument(); // token_count
    expect(screen.getByText("47")).toBeInTheDocument(); // memory_count
    expect(screen.getByText("v3")).toBeInTheDocument(); // schema version
  });

  it("does not render a 'Manage' deep-link (the Tokens tab replaces it)", () => {
    render(<ResourceStatsStrip resource={make()} />);
    expect(
      screen.queryByRole("link", { name: /stats\.manage/ }),
    ).not.toBeInTheDocument();
  });

  it("surfaces the absolute timestamp as a tooltip on Last Activity", () => {
    render(<ResourceStatsStrip resource={make()} />);
    const lastActivityValue = screen.getByText(
      "relative(2026-04-14T09:15:30Z)",
    );
    expect(lastActivityValue).toHaveAttribute(
      "title",
      "dt(2026-04-14T09:15:30Z)",
    );
  });

  it("renders em-dash and 'not registered' when no schema version exists", () => {
    render(
      <ResourceStatsStrip resource={make({ current_schema_version: null })} />,
    );
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText("stats.noSchemaYet")).toBeInTheDocument();
  });

  it("renders last-activity via formatRelativeTime", () => {
    render(<ResourceStatsStrip resource={make()} />);
    expect(
      screen.getByText("relative(2026-04-14T09:15:30Z)"),
    ).toBeInTheDocument();
  });
});
