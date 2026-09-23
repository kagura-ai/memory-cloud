"use client";

/**
 * useCanUpgrade (#1643)
 *
 * "May we show this member an upgrade CTA on this deployment?"
 *
 * Every plan / quota surface in the UI sends the user to
 * `/workspace/settings/plan`. That page is behind the `plan_page` deployment
 * flag (default OFF for OSS / self-hosted, `backend/src/config/settings.py:745`)
 * and its data call is owner-only (#246). The Sidebar already encodes exactly
 * this pair — `requiredFeature: "plan_page"` + `requiredWorkspaceRole: Owner` —
 * so this hook is that same rule, made reusable, and the CTA now appears in
 * precisely the deployments where the nav entry does.
 *
 * Tri-state, matching `usePlanFeature`'s contract:
 *   true  — the Plan page exists here and this member can load it
 *   false — it does not exist here, or this member is not the owner
 *   null  — not known yet; withhold the CTA, never flash one
 *
 * Callers render the actionable element on `=== true` only. The explanatory
 * copy around it is NOT conditional — the feature really is unavailable, so
 * the notice stays; only the thing that would dead-end goes.
 *
 * Transport failure resolves to `false`, not `null`, because `useSystemFeatures`
 * fails closed (`useSystemFeatures.ts` → `FAILED_INFO` with `features: {}`).
 * That is deliberate and is the opposite of `usePlanFeatures`' fail-pending: a
 * withheld CTA costs a click, a dead-ending CTA costs trust.
 */

import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import type { SystemFeatures } from "@/lib/api/system";

/**
 * Pure rule, exported for unit tests and for #1645's `useFeatureGate`, which
 * already holds `/system/info` and the workspace and must not re-derive this.
 * Mirrors `planFeaturesFor` (usePlanFeatures.ts) as the pure half of a hook.
 */
export function canUpgradeFrom(
  features: SystemFeatures | null | undefined,
  workspaceLoading: boolean,
  role: string | null | undefined,
): boolean | null {
  // `!features` (not `=== null`): a test double may hand back `undefined`, and
  // "no features object" is the same pending signal either way.
  if (!features) return null;
  // Checked BEFORE the workspace resolves: on a default self-hosted deployment
  // the answer is a definitive `false` from the first render, with no pending
  // window at all.
  if (features.plan_page !== true) return false;
  if (workspaceLoading) return null;
  // Fail closed on data: an API build that omits `current_user_role`
  // (`workspaces.ts`, optional) reads as "not owner" — the same call the
  // Sidebar already makes for the Plan nav entry, so CTA and nav entry can
  // never disagree.
  return hasWorkspaceRole(role, WorkspaceRole.Owner);
}

/** @see canUpgradeFrom */
export function useCanUpgrade(): boolean | null {
  const features = useSystemFeatures();
  const { currentWorkspace, loading } = useWorkspace();
  return canUpgradeFrom(
    features,
    loading === true,
    currentWorkspace?.current_user_role,
  );
}
