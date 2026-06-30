/**
 * Tests for useSystemFeatures (#1145).
 *
 * Directly exercises the hook (the Sidebar/plan-page suites mock it away), so
 * the eager-on-mount fetch + the fail-closed error fallback are actually
 * covered. Each test gets a fresh module so the module-level cache doesn't leak
 * across cases.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});

async function setup(getSystemInfo: () => Promise<unknown>) {
  vi.doMock("@/lib/api/system", () => ({ getSystemInfo }));
  const { useSystemFeatures } = await import("./useSystemFeatures");
  function Harness() {
    const f = useSystemFeatures();
    return (
      <div data-testid="out">
        {f ? `loaded:${JSON.stringify(f)}` : "loading"}
      </div>
    );
  }
  return Harness;
}

describe("useSystemFeatures (#1145)", () => {
  it("fetches /system/info on mount and exposes its features", async () => {
    const getSystemInfo = vi
      .fn()
      .mockResolvedValue({ features: { plan_page: true } });
    const Harness = await setup(getSystemInfo);

    render(<Harness />);
    // Eager fetch on first mount (not lazy).
    expect(getSystemInfo).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByTestId("out").textContent).toContain("plan_page"),
    );
    expect(screen.getByTestId("out").textContent).toContain("true");
  });

  it("returns empty features (fail-closed / default-off) when the fetch fails", async () => {
    const getSystemInfo = vi.fn().mockRejectedValue(new Error("network"));
    const Harness = await setup(getSystemInfo);

    render(<Harness />);
    // Error → {} so every gated feature reads as disabled.
    await waitFor(() =>
      expect(screen.getByTestId("out").textContent).toBe("loaded:{}"),
    );
  });
});
