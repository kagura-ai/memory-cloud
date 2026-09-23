"use client";

/**
 * useFeatureGate / useFeatureGates (#1645)
 *
 * The one pre-check answer to "may this member use X here, and if not, why?"
 * — a `FeatureGate` descriptor whose `state` is `pending`, `allowed` or the
 * refusal reason (`plan`, `deployment`, `role`, `quota`).
 *
 * The React binding for `resolveGate` (`lib/gates/featureGates.ts`, where the
 * rules and the two failure directions are documented). It subscribes once to
 * each input — the shared tier matrix, `/system/info`, the workspace and the
 * locale — all module-cached, so any number of gates costs no extra request.
 *
 * `canUpgrade` comes from the pure `canUpgradeFrom` (`hooks/useCanUpgrade.ts`),
 * not a second copy of the rule: this hook already holds `/system/info` and
 * the workspace. The raw answer is narrowed by state inside `resolveGate`.
 *
 * Callers branch on `gate.state`. Never render an upsell for `pending`: it is
 * what a still-loading — or failing — tier matrix looks like.
 */

import { useMemo } from "react";
import { useLocale } from "next-intl";

import { useWorkspace } from "@/contexts/WorkspaceContext";
import { canUpgradeFrom } from "@/hooks/useCanUpgrade";
import { usePlanTierMatrix } from "@/hooks/usePlanFeatures";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import {
  resolveGate,
  type FeatureGate,
  type GateKey,
} from "@/lib/gates/featureGates";

/** Page-local counts for a gate that is also capped. */
export interface GateQuota {
  readonly current: number;
  readonly limit: number;
}

/**
 * Several gates from one set of subscriptions. The plural form exists for a
 * fixed set read inside a `.filter()` or a form, where one hook call per item
 * would be illegal. `keys` may be a fresh array each render.
 */
export function useFeatureGates<K extends GateKey>(
  keys: readonly K[],
  opts?: { quotas?: Partial<Record<K, GateQuota>> },
): Readonly<Record<K, FeatureGate>> {
  const tiers = usePlanTierMatrix();
  const features = useSystemFeatures();
  const { currentWorkspace, loading } = useWorkspace();
  const locale = useLocale();

  // `!== null`, NOT `!loading`: a user with no workspace has `loading ===
  // false` and no workspace forever, and must stay pending, never upsold.
  // Byte-identical to `usePlanFeatures`.
  const workspaceResolved = currentWorkspace !== null;
  const planName = currentWorkspace?.plan_name;
  const role = currentWorkspace?.current_user_role;
  const canUpgrade = canUpgradeFrom(features, loading === true, role) === true;

  // Content signatures, so a caller's inline array / object literal does not
  // defeat the memo (and hand consumers a new descriptor every render).
  const keySignature = keys.join("|");
  const quotas = opts?.quotas;
  const quotaSignature = quotas
    ? JSON.stringify(keys.map((key) => quotas[key] ?? null))
    : "";

  return useMemo(
    () => {
      const gates = {} as Record<K, FeatureGate>;
      for (const key of keys) {
        gates[key] = resolveGate({
          key,
          tiers,
          planName,
          workspaceResolved,
          // A test double may hand back `undefined`; that is "unresolved" too.
          features: features ?? null,
          role,
          canUpgrade,
          locale,
          quota: quotas?.[key],
        });
      }
      return gates;
    },
    // `keys` / `quotas` are read through their signatures above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      keySignature,
      quotaSignature,
      tiers,
      features,
      workspaceResolved,
      planName,
      role,
      canUpgrade,
      locale,
    ],
  );
}

/** One gate. Same subscriptions and answer as `useFeatureGates([key])[key]`. */
export function useFeatureGate(
  key: GateKey,
  opts?: { quota?: GateQuota },
): FeatureGate {
  const quota = opts?.quota;
  return useFeatureGates(
    [key],
    quota ? { quotas: { [key]: quota } } : undefined,
  )[key];
}
