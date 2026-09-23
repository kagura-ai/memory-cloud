"use client";

/**
 * useErrorGate (#1644)
 *
 * The React binding for a refusal the server just sent: an `ApiError`'s
 * normalised gate facts (`err.gate`, set once in `lib/api/base.ts`) lifted to
 * a full `FeatureGate` — the feature key resolved, the tier labels resolved
 * for this locale, and `canUpgrade` answered.
 *
 * `canUpgrade` is NOT decided here. This hook supplies only the RAW answer —
 * `canUpgradeFrom(...) === true` (useCanUpgrade.ts: Plan page on this
 * deployment AND this member is the owner, pending collapsing to `false`) —
 * and `gateFromFacts` narrows it by state through `narrowCanUpgrade`. That is
 * the one place the rule lives: allowlist, deployment and role gates never
 * carry an upgrade CTA, and neither does a quota gate no higher tier lifts.
 *
 * The `instanceof ApiError` check lives here, not in `lib/gates`, because
 * `lib/api/base.ts` imports `normalizeGate` from `lib/gates/featureGates.ts`;
 * the reverse import would be a cycle.
 *
 * Returns `null` for anything that is not a gate refusal — a non-ApiError, or
 * an ApiError with no gate (a bare 403/429 included) — so the caller keeps its
 * own handling for those.
 */

import { useLocale } from "next-intl";

import { useWorkspace } from "@/contexts/WorkspaceContext";
import { canUpgradeFrom } from "@/hooks/useCanUpgrade";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { ApiError } from "@/lib/api/base";
import {
  gateFromFacts,
  type FeatureGate,
  type GateKey,
} from "@/lib/gates/featureGates";

export function useErrorGate(
  err: unknown,
  fallbackKey: GateKey,
): FeatureGate | null {
  // Hooks first, unconditionally — the early return below must not change
  // the hook order between renders.
  const features = useSystemFeatures();
  const { currentWorkspace, loading } = useWorkspace();
  const locale = useLocale();

  if (!(err instanceof ApiError)) return null;

  const raw =
    canUpgradeFrom(
      features,
      loading === true,
      currentWorkspace?.current_user_role,
    ) === true;
  return gateFromFacts(err.gate, { fallbackKey, canUpgrade: raw, locale });
}
