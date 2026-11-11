/**
 * The system-admin menu lists only system-admin functions (#2764).
 *
 * The memory health report belongs to the context it describes (the Health
 * tab of the context detail), so neither its entry nor its old URL is in the
 * route catalog the sidebar and the route guard read.
 */

import { describe, expect, it } from "vitest";

import {
  navigationForPath,
  navigationGroups,
  sidebarNavigationGroups,
} from "./featureNavigation";

const allItems = navigationGroups.flatMap((group) =>
  group.items.flatMap((item) => [item, ...(item.children ?? [])]),
);

describe("system-admin navigation", () => {
  it("has no memory health entry (#2764)", () => {
    expect(allItems.map((item) => item.nameKey)).not.toContain("memoryHealth");
    expect(allItems.map((item) => item.href)).not.toContain("/admin/memory-health");
    const sidebarHrefs = sidebarNavigationGroups.flatMap((g) => g.items.map((i) => i.href));
    expect(sidebarHrefs).not.toContain("/admin/memory-health");
    expect(navigationForPath("/admin/memory-health")).toBeUndefined();
  });
});
