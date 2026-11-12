/**
 * The system-admin menu lists only system-admin functions (#2764, #2765).
 *
 * The memory health report belongs to the context it describes (the Health
 * tab of the context detail), so neither its entry nor its old URL is in the
 * route catalog the sidebar and the route guard read. Plans are managed from
 * the admin user detail and the Users page (#2765), so the Quotas entry and
 * its URLs are gone too.
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
    expect(allItems.map((item) => item.href)).not.toContain(
      "/admin/memory-health",
    );
    const sidebarHrefs = sidebarNavigationGroups.flatMap((g) =>
      g.items.map((i) => i.href),
    );
    expect(sidebarHrefs).not.toContain("/admin/memory-health");
    expect(navigationForPath("/admin/memory-health")).toBeUndefined();
  });

  it("has no Quotas entry (#2765)", () => {
    expect(allItems.map((item) => item.nameKey)).not.toContain("quotas");
    const hrefs = allItems.map((item) => item.href.split("?")[0]);
    expect(hrefs).not.toContain("/admin/plans");
    expect(hrefs).not.toContain("/admin/quotas");
    expect(navigationForPath("/admin/plans")).toBeUndefined();
    expect(navigationForPath("/admin/quotas")).toBeUndefined();
  });
});

describe("system-admin surface flag (#2767)", () => {
  it("every admin item needs the system role and features.system_admin", () => {
    const adminGroup = navigationGroups.find((group) => group.titleKey === "admin");
    expect(adminGroup?.items.length).toBeGreaterThan(0);
    for (const item of adminGroup?.items ?? []) {
      expect(item.href.startsWith("/admin/")).toBe(true);
      expect(item.requiredRole).toBe("admin");
      expect([item.requiredFeature].flat()).toContain("system_admin");
    }
  });

  it("no other item carries the system_admin flag", () => {
    const others = navigationGroups
      .filter((group) => group.titleKey !== "admin")
      .flatMap((group) => group.items.flatMap((item) => [item, ...(item.children ?? [])]));
    for (const item of others) {
      expect([item.requiredFeature].flat()).not.toContain("system_admin");
    }
  });
});
