"use client";

/**
 * #1939: app-wide notice that paid-only features are paused on the current
 * workspace (its plan has no allowance for them, e.g. back on Free). The plan endpoint is owner-only, so only
 * owners — the people who can re-subscribe — fetch it and see the notice.
 * Hidden on the plan page itself, which renders the full banner. A failed
 * fetch shows nothing: this is an informational notice, not a page error.
 */

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { getWorkspacePlan } from "@/lib/api/workspaces";
import { SuspendedFeaturesBanner } from "./SuspendedFeaturesBanner";

const PLAN_PAGE = "/workspace/settings/plan";

export function WorkspaceSuspensionNotice() {
  const { currentWorkspaceId, currentWorkspace } = useWorkspace();
  const systemFeatures = useSystemFeatures();
  const pathname = usePathname();
  const [suspended, setSuspended] = useState<string[]>([]);

  const isOwner = currentWorkspace?.current_user_role === "owner";
  const onPlanPage = pathname === PLAN_PAGE;
  const enabled = Boolean(
    currentWorkspaceId && isOwner && systemFeatures?.plan_page && !onPlanPage,
  );

  useEffect(() => {
    if (!enabled || !currentWorkspaceId) {
      setSuspended([]);
      return;
    }
    let cancelled = false;
    getWorkspacePlan(currentWorkspaceId)
      .then((plan) => {
        if (!cancelled) setSuspended(plan.suspended ?? []);
      })
      .catch(() => {
        if (!cancelled) setSuspended([]);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, currentWorkspaceId]);

  if (onPlanPage || suspended.length === 0) return null;
  return (
    <div className="border-b border-amber-200 px-4 py-2 dark:border-amber-900">
      <SuspendedFeaturesBanner suspended={suspended} showPlanLink />
    </div>
  );
}
