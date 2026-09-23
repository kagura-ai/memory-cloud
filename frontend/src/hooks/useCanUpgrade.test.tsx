/**
 * Tests for useCanUpgrade (#1643).
 *
 * `canUpgradeFrom` is pure, so the truth table is exercised directly; the hook
 * itself only needs one wiring test proving it feeds the pure rule from
 * `useSystemFeatures` + `WorkspaceContext`.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { canUpgradeFrom } from "./useCanUpgrade";

let mockFeatures: Record<string, boolean> | null = { plan_page: true };
let mockWorkspace: {
  currentWorkspace: { current_user_role?: string | null } | null;
  loading: boolean;
} = {
  currentWorkspace: { current_user_role: "owner" },
  loading: false,
};

vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockWorkspace,
}));

describe("canUpgradeFrom (#1643)", () => {
  it("returns null while /system/info is unresolved", () => {
    expect(canUpgradeFrom(null, false, "owner")).toBeNull();
  });

  it("returns null when the features object is undefined", () => {
    expect(canUpgradeFrom(undefined, false, "owner")).toBeNull();
  });

  it("returns false when plan_page is off, without waiting for the workspace", () => {
    // workspaceLoading === true and the answer is still a definitive false:
    // the OSS default never goes through a pending window.
    expect(canUpgradeFrom({ plan_page: false }, true, "owner")).toBe(false);
  });

  it("returns false when plan_page is absent (older backend / failed fetch)", () => {
    expect(canUpgradeFrom({}, false, "owner")).toBe(false);
  });

  it("returns null while the workspace is still loading", () => {
    expect(canUpgradeFrom({ plan_page: true }, true, undefined)).toBeNull();
  });

  it("returns true for an owner on a plan_page deployment", () => {
    expect(canUpgradeFrom({ plan_page: true }, false, "owner")).toBe(true);
  });

  it.each(["admin", "member", "viewer"])("returns false for %s", (role) => {
    expect(canUpgradeFrom({ plan_page: true }, false, role)).toBe(false);
  });

  it("returns false when current_user_role is missing", () => {
    expect(canUpgradeFrom({ plan_page: true }, false, undefined)).toBe(false);
    expect(canUpgradeFrom({ plan_page: true }, false, null)).toBe(false);
  });
});

describe("useCanUpgrade (#1643)", () => {
  beforeEach(() => {
    mockFeatures = { plan_page: true };
    mockWorkspace = {
      currentWorkspace: { current_user_role: "owner" },
      loading: false,
    };
  });

  async function renderHarness() {
    const { useCanUpgrade } = await import("./useCanUpgrade");
    function Harness() {
      const canUpgrade = useCanUpgrade();
      return <div data-testid="out">{String(canUpgrade)}</div>;
    }
    render(<Harness />);
    return screen.getByTestId("out").textContent;
  }

  it("composes useSystemFeatures and the workspace role", async () => {
    expect(await renderHarness()).toBe("true");
  });

  it("is false for a non-owner on a plan_page deployment", async () => {
    mockWorkspace = {
      currentWorkspace: { current_user_role: "admin" },
      loading: false,
    };
    expect(await renderHarness()).toBe("false");
  });

  it("is false when the deployment has no Plan page", async () => {
    mockFeatures = {};
    expect(await renderHarness()).toBe("false");
  });

  it("is null while /system/info is unresolved", async () => {
    mockFeatures = null;
    expect(await renderHarness()).toBe("null");
  });

  it("is null while the workspace is hydrating", async () => {
    mockWorkspace = { currentWorkspace: null, loading: true };
    expect(await renderHarness()).toBe("null");
  });
});
