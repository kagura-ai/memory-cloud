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
 * Fail-closed on DATA: a plan name missing from the matrix or a boolean an
 * older API omits reads as "not included", mirroring the backend's free-tier
 * fallback for unrecognised plan names. Fail-PENDING on TRANSPORT: when the
 * fetch keeps failing the hook stays `null` instead of resolving to `false`,
 * because "matrix unavailable" must never upsell an entitled tenant or fire a
 * consumer's not-included branch (the connectors page strips its one-time
 * Slack install handle on `false`). The failure is not cached, so the next
 * mount retries.
 */

import { useEffect, useMemo, useState } from "react";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { getPlanTierMatrix, type PlanTierFeature } from "@/lib/api/workspaces";

/**
 * Every boolean column of the served tier row (#1551 create gates, #1583
 * `shared_contexts`, and — since #1645 — the rest: `team_invitations`,
 * `reranking`, `managed_embeddings`, `managed_llm`, `secret_store`).
 *
 * Derived from `PlanTierFeature` at compile time, not hand-typed: the API
 * type is the source of truth. `-?` + `NonNullable` keep the optional
 * `managed_llm?` (absent on an API predating #1569) in the union.
 */
export type PlanFeature = {
  [K in keyof PlanTierFeature]-?: NonNullable<PlanTierFeature[K]> extends boolean
    ? K
    : never;
}[keyof PlanTierFeature];

export type PlanFeatures = Readonly<Record<PlanFeature, boolean>>;

/**
 * The runtime list of `PlanFeature`. The assertion under it fails `tsc` when a
 * boolean column is added to `PlanTierFeature` and not listed here, so the
 * lookup below can never silently skip one.
 */
export const PLAN_FEATURE_KEYS = [
  "resources",
  "connectors",
  "public_contexts",
  "shared_contexts",
  "team_invitations",
  "reranking",
  "managed_embeddings",
  "managed_llm",
  "secret_store",
] as const satisfies readonly PlanFeature[];

type AssertNever<T extends never> = T;
type _EveryPlanFeatureListed = AssertNever<
  Exclude<PlanFeature, (typeof PLAN_FEATURE_KEYS)[number]>
>;

function featuresFrom(read: (key: PlanFeature) => boolean): PlanFeatures {
  return Object.fromEntries(
    PLAN_FEATURE_KEYS.map((key) => [key, read(key)]),
  ) as Record<PlanFeature, boolean>;
}

const NO_FEATURES: PlanFeatures = featuresFrom(() => false);

/**
 * Pure lookup: the plan features for `planName` in `tiers`. Unknown plan →
 * every gate false. Exported for unit tests and for callers that already
 * hold the matrix (e.g. an admin view iterating every tier).
 */
export function planFeaturesFor(
  tiers: readonly PlanTierFeature[],
  planName: string | null | undefined,
): PlanFeatures {
  const tier = tiers.find((t) => t.name === planName);
  if (!tier) return NO_FEATURES;
  // `=== true` so an API predating a column (field absent) fails closed.
  return featuresFrom((key) => tier[key] === true);
}

// Same retry shape as useSystemFeatures, but unlike that hook a persistent
// failure does NOT fail closed: the hook stays `null` (pending) rather than
// resolving to a terminal "not included" that would show an upsell to an
// entitled tenant — see the docblock.
const MAX_ATTEMPTS = 3;
const RETRY_BASE_MS = 500;

let cache: PlanTierFeature[] | null = null;
let inflight: Promise<PlanTierFeature[] | null> | null = null;

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

/** What `usePlanTierMatrixState` reports. */
export interface PlanTierMatrixState {
  /** The shared matrix, `null` until the first fetch resolves. */
  readonly tiers: PlanTierFeature[] | null;
  /**
   * True once the retried fetch has definitively failed (and nothing is
   * cached). Only a surface that OWNS an error UI reads this — the gate hooks
   * deliberately ignore it, because a failure must read as pending there.
   */
  readonly failed: boolean;
}

/**
 * The shared tier matrix plus whether its retried fetch has failed, over the
 * same module cache as `usePlanTierMatrix` (#1645). The Plan page's
 * comparison table reads it so it can keep an error banner instead of a
 * loader that never resolves; a later mount still retries, because the
 * failure is not cached.
 */
export function usePlanTierMatrixState(): PlanTierMatrixState {
  const [state, setState] = useState<PlanTierMatrixState>(() => ({
    tiers: cache,
    failed: false,
  }));

  useEffect(() => {
    if (cache) {
      const cached = cache;
      setState((prev) =>
        prev.tiers === cached && !prev.failed
          ? prev
          : { tiers: cached, failed: false },
      );
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
          return null;
        });
    }
    let alive = true;
    inflight.then((data) => {
      if (alive) setState({ tiers: data, failed: data === null });
    });
    return () => {
      alive = false;
    };
  }, []);

  return state;
}

/**
 * The cached tier matrix, `null` until the first fetch resolves. A persistent
 * failure also leaves it `null` (consumers stay pending, never upsell) and is
 * not cached, so a later mount retries.
 */
export function usePlanTierMatrix(): PlanTierFeature[] | null {
  return usePlanTierMatrixState().tiers;
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
