/**
 * Tests for useWorkspaceObjectPresence (#1571, externalKeys kind #1616).
 *
 * The Sidebar suite exercises the nav rule with the list APIs mocked; this
 * pins the hook's own contract — tri-state answer, per-workspace cache, no
 * probe while disabled, failure not cached — and above all that a workspace
 * switch reads `null` in the SAME render. A stale `true` for one frame is
 * exactly the flash-then-hide the rule exists to prevent, and it is invisible
 * to an assertion made after effects have flushed, so the switch case logs
 * every render's answer. Each test gets a fresh module so the module-level
 * cache does not leak across cases.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});

/** A promise the test settles by hand, so "before the probe lands" is exact. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type ResourceList = { resources: never[]; total: number };
const resources = (total: number): ResourceList => ({ resources: [], total });

async function setup(
  listResources: ReturnType<typeof vi.fn>,
  listConnectors: ReturnType<typeof vi.fn> = vi.fn(),
  listExternalAPIKeys: ReturnType<typeof vi.fn> = vi.fn(),
) {
  vi.doMock("@/lib/api/resources", () => ({ listResources }));
  vi.doMock("@/lib/api/workspace-connectors", () => ({ listConnectors }));
  vi.doMock("@/lib/api/external-keys", () => ({ listExternalAPIKeys }));
  const { useWorkspaceObjectPresence } =
    await import("./useWorkspaceObjectPresence");
  return useWorkspaceObjectPresence;
}

type Props = { workspaceId: string | null; enabled: boolean };

describe("useWorkspaceObjectPresence (#1571)", () => {
  it("is null while probing, then answers whether the workspace owns any", async () => {
    const probe = deferred<ResourceList>();
    const listResources = vi.fn().mockReturnValueOnce(probe.promise);
    const usePresence = await setup(listResources);

    const { result } = renderHook(() => usePresence("resources", "w1", true));
    expect(result.current).toBeNull();
    expect(listResources).toHaveBeenCalledTimes(1);

    await act(async () => probe.resolve(resources(2)));
    expect(result.current).toBe(true);
  });

  it("answers false for a workspace that owns none (connectors: list length)", async () => {
    const listConnectors = vi.fn().mockResolvedValue([]);
    const usePresence = await setup(vi.fn(), listConnectors);

    const { result } = renderHook(() => usePresence("connectors", "w1", true));
    await waitFor(() => expect(result.current).toBe(false));
    expect(listConnectors).toHaveBeenCalledTimes(1);
  });

  it("never probes and reads null while disabled; probes once enabled", async () => {
    const listResources = vi.fn().mockResolvedValue(resources(1));
    const usePresence = await setup(listResources);

    const { result, rerender } = renderHook(
      ({ workspaceId, enabled }: Props) =>
        usePresence("resources", workspaceId, enabled),
      { initialProps: { workspaceId: "w1", enabled: false } },
    );
    expect(result.current).toBeNull();
    expect(listResources).not.toHaveBeenCalled();

    // The plan resolved to "not included" → now the fallback matters.
    rerender({ workspaceId: "w1", enabled: true });
    expect(listResources).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(result.current).toBe(true));
  });

  it("reads null in the very render that switches workspace — never the previous answer", async () => {
    const second = deferred<ResourceList>();
    const listResources = vi
      .fn()
      .mockResolvedValueOnce(resources(1))
      .mockReturnValueOnce(second.promise);
    const usePresence = await setup(listResources);

    const seen: Array<boolean | null> = [];
    const { result, rerender } = renderHook(
      ({ workspaceId, enabled }: Props) => {
        const value = usePresence("resources", workspaceId, enabled);
        seen.push(value);
        return value;
      },
      { initialProps: { workspaceId: "w1", enabled: true } },
    );
    await waitFor(() => expect(result.current).toBe(true));

    // Low-tier A (owns a resource) → low-tier B (unknown yet). Every render
    // from the switch on must read null; `[true, null]` here is the flash.
    seen.length = 0;
    rerender({ workspaceId: "w2", enabled: true });
    expect(seen.length).toBeGreaterThan(0);
    expect(seen.every((value) => value === null)).toBe(true);
    expect(listResources).toHaveBeenCalledTimes(2);

    await act(async () => second.resolve(resources(0)));
    expect(result.current).toBe(false);

    // Back to A: answered from the cache in the same render, no third probe.
    seen.length = 0;
    rerender({ workspaceId: "w1", enabled: true });
    expect(seen).toEqual([true]);
    expect(listResources).toHaveBeenCalledTimes(2);
  });

  it("externalKeys: answers from the owner-only key list (#1616)", async () => {
    // The External Keys entry stays for a workspace that stored keys before
    // ENABLE_BYOK was turned off; the list route stays open for the owner.
    const listExternalAPIKeys = vi
      .fn()
      .mockResolvedValueOnce([{ key_name: "OPENAI_API_KEY" }])
      .mockResolvedValueOnce([]);
    const usePresence = await setup(vi.fn(), vi.fn(), listExternalAPIKeys);

    const withKey = renderHook(() => usePresence("externalKeys", "w1", true));
    await waitFor(() => expect(withKey.result.current).toBe(true));

    const without = renderHook(() => usePresence("externalKeys", "w2", true));
    await waitFor(() => expect(without.result.current).toBe(false));
    expect(listExternalAPIKeys).toHaveBeenCalledTimes(2);
    // The probe wants every key, not a provider subset.
    expect(listExternalAPIKeys).toHaveBeenCalledWith();
  });

  it("a failed probe reads null, is not cached, and the next mount retries", async () => {
    const listResources = vi
      .fn()
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValueOnce(resources(1));
    const usePresence = await setup(listResources);

    const first = renderHook(() => usePresence("resources", "w1", true));
    expect(listResources).toHaveBeenCalledTimes(1);
    // Let the rejection settle; the answer stays unknown (entry hidden).
    await act(async () => {});
    expect(first.result.current).toBeNull();

    first.unmount();
    const second = renderHook(() => usePresence("resources", "w1", true));
    expect(listResources).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(second.result.current).toBe(true));
  });
});
