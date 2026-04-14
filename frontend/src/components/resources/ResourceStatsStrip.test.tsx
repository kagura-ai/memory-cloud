/**
 * Tests for ResourceStatsStrip.
 *
 * Verifies:
 * - all four KpiCards render with expected values
 * - "Manage" deep-link URL contains encoded resource_id
 * - schema_version === null renders em-dash + "not registered" subtext
 * - schema_version > 0 renders "v<version>"
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ResourceStatsStrip } from "./ResourceStatsStrip";
import type { ResourceListItem } from "@/lib/api/resources";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatRelativeTime: (iso: string) => `relative(${iso})`,
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

  it("links 'Manage' to credentials page with encoded resource_id", () => {
    render(
      <ResourceStatsStrip
        resource={make({ resource_id: "has spaces/weird" })}
      />,
    );
    const link = screen.getByRole("link", { name: /stats\.manage/ });
    expect(link).toHaveAttribute(
      "href",
      "/workspace/integrations/credentials?tab=resource-tokens&resource_id=has%20spaces%2Fweird",
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
