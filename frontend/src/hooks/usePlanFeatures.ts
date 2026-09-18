"use client";

/**
 * usePlanFeatures (#1560)
 *
 * Answers "may this workspace CREATE X?" from the plan API instead of a
 * hardcoded tier check. The booleans come from `GET /workspaces/plans/tiers`
 * (`PlanTierFeature.resources / connectors / public_contexts`, #1551), looked
 * up by the current workspace's `plan_name`, so the UI stops knowing which
 * tier names exist or how they rank — which keeps #1394 (admin-definable
 * tiers) cheap. `planAtLeast` remains the tool for genuinely ordinal gates.
 *
 * Module-cached like `useSystemFeatures`: every consumer shares one fetch per
 * session (the matrix is public reference data, readable by any session
 * user). Returns `null` while the matrix or the workspace is still resolving.
 * Callers must NOT flash an upsell in that window — keep the create control
 * pending (hidden / disabled / skeleton) until the answer is known.
 *
 * Fail-closed: a plan name missing from the matrix, a boolean an older API
 * omits, or a persistent fetch failure all read as "not included", mirroring
 * the backend's free-tier fallback for unrecognised plan names.
 */

import { useEffect, useMemo, useState } from "react";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { getPlanTierMatrix, type PlanTierFeature } from "@/lib/api/workspaces";

/** The #1551 "may create" gates exposed as booleans on each tier. */
export type PlanFeature = "resources" | "connectors" | "public_contexts";

export type PlanFeatures = Readonly<Record<PlanFeature, boolean>>;

const NO_FEATURES: PlanFeatures = {
  resources: false,
  connectors: false,
  public_contexts: false,
};

/**
 * Pure lookup: the create gates for `planName` in `tiers`. Unknown plan →
 * every gate false. Exported for unit tests and for callers that already
 * hold the matrix (e.g. an admin view iterating every tier).
 */
export function planFeaturesFor(
  tiers: readonly PlanTierFeature[],
  planName: string | null | undefined,
): PlanFeatures {
  const tier = tiers.find((t) => t.name === planName);
  if (!tier) return NO_FEATURES;
  // `=== true` so an API predating #1551 (field absent) fails closed.
  return {
    resources: tier.resources === true,
    connectors: tier.connectors === true,
    public_contexts: tier.public_contexts === true,
  };
}

// Same retry-then-fail-closed shape as useSystemFeatures: a transient blip
// keeps the hook in the `null` (pending) state instead of resolving to a
// terminal "not included" that would show an upsell to an entitled tenant.
const MAX_ATTEMPTS = 3;
const RETRY_BASE_MS = 500;

let cache: PlanTierFeature[] | null = null;
let inflight: Promise<PlanTierFeature[]> | null = null;

async function fetchMatrixWithRetry(): Promise<PlanTierFeature[]> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      return await getPlanTierMatrix();
    } catch (e) {
      lastError = e;
      if (attempt < MAX_ATTEMPTS) {
        await new Promise((resolve) =>
          setTimeout(resolve, RETRY_BASE_MS * attempt),
        );
      }
    }
  }
  throw lastError;
}

/**
 * The cached tier matrix, `null` until the first fetch resolves. A persistent
 * failure yields `[]` (every plan unknown → every gate false) and is not
 * cached, so a later mount retries.
 */
export function usePlanTierMatrix(): PlanTierFeature[] | null {
  const [tiers, setTiers] = useState<PlanTierFeature[] | null>(cache);

  useEffect(() => {
    if (cache) {
      setTiers(cache);
      return;
    }
    if (!inflight) {
      inflight = fetchMatrixWithRetry()
        .then((data) => {
          cache = data;
          return cache;
        })
        .catch((e) => {
          if (process.env.NODE_ENV === "development") {
            // eslint-disable-next-line no-console
            console.error("usePlanTierMatrix: /plans/tiers fetch failed", e);
          }
          inflight = null;
          return [] as PlanTierFeature[];
        });
    }
    let alive = true;
    inflight.then((data) => {
      if (alive) setTiers(data);
    });
    return () => {
      alive = false;
    };
  }, []);

  return tiers;
}

/**
 * Create gates for the current workspace's plan, or `null` while unknown
 * (matrix still loading, or no workspace resolved yet).
 */
export function usePlanFeatures(): PlanFeatures | null {
  const { currentWorkspace } = useWorkspace();
  const tiers = usePlanTierMatrix();
  const planName = currentWorkspace?.plan_name;
  const ready = tiers !== null && currentWorkspace !== null;

  return useMemo(
    () => (ready && tiers ? planFeaturesFor(tiers, planName) : null),
    [ready, tiers, planName],
  );
}

/**
 * One create gate for the current workspace, as a tri-state: `true` (may
 * create), `false` (show the upsell), `null` (still resolving — keep the
 * control pending, never upsell). Callers must compare with `=== false`
 * before rendering an upsell so the pending state cannot flash one.
 */
export function usePlanFeature(feature: PlanFeature): boolean | null {
  const features = usePlanFeatures();
  return features === null ? null : features[feature];
}
