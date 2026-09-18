"use client";

/**
 * useWorkspaceObjectPresence (#1571)
 *
 * "Does the current workspace already own at least one resource / connector?"
 * — the fallback the sidebar needs for a plan-gated nav entry. Since #1551 a
 * plan without `resources` / `connectors` refuses NEW ones, but objects
 * created before a downgrade keep working, so their entry must stay reachable.
 *
 * Probes only while `enabled` (the caller passes "the plan says no AND my role
 * may list") — an included plan never pays the extra request. Answers are
 * module-cached per workspace + kind for the session: the count cannot grow
 * while the plan refuses creation, and a plan upgrade flips the gate itself.
 *
 * Tri-state like `usePlanFeature`: `true` / `false` once known, `null` while
 * resolving or disabled. A failed probe also reads `null` (the entry stays
 * hidden; the page itself is still reachable by URL) and is not cached, so
 * the next mount retries.
 */

import { useEffect, useState } from "react";
import { listResources } from "@/lib/api/resources";
import { listConnectors } from "@/lib/api/workspace-connectors";

/** The plan-gated object kinds the sidebar has a nav entry for. */
export type WorkspaceObjectKind = "resources" | "connectors";

const PROBES: Record<WorkspaceObjectKind, () => Promise<boolean>> = {
  resources: async () => (await listResources()).total > 0,
  connectors: async () => (await listConnectors()).length > 0,
};

const cache = new Map<string, boolean>();

export function useWorkspaceObjectPresence(
  kind: WorkspaceObjectKind,
  workspaceId: string | null | undefined,
  enabled: boolean,
): boolean | null {
  const key = workspaceId ? `${workspaceId}:${kind}` : null;
  // The resolved answer is stored WITH the key it belongs to. The sidebar's
  // nav filter runs during render, so an effect-time reset (the contextCount
  // pattern) is one commit too late: the render that switches `key` would
  // still read the previous workspace's `true` and flash its entry for a
  // frame. Keyed state makes a key change read `null` — or that workspace's
  // cached answer — in the very same render.
  const [resolved, setResolved] = useState<{
    key: string;
    has: boolean;
  } | null>(null);

  useEffect(() => {
    if (!enabled || key === null || cache.has(key)) return;

    let cancelled = false;
    PROBES[kind]()
      .then((has) => {
        cache.set(key, has);
        if (!cancelled) setResolved({ key, has });
      })
      .catch(() => {
        // Unknown, not cached — the return below keeps reading `null` for
        // this key and the next mount retries.
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, key, kind]);

  if (!enabled || key === null) return null;
  if (resolved?.key === key) return resolved.has;
  return cache.get(key) ?? null;
}
