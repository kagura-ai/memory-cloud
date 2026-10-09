"use client";

/**
 * #1939: app-wide notice that paid-only features are paused on the current
 * workspace (its plan has no allowance for them, e.g. back on Free). The plan endpoint is owner-only, so only
 * owners — the people who can re-subscribe — fetch it and see the notice.
 * Hidden on the plan page itself, which renders the full banner. A failed
 * fetch shows nothing: this is an informational notice, not a page error.
 * While the notice is showing it re-reads the plan on every navigation, so it
 * clears without a reload once the owner re-subscribes; while nothing is
 * paused, navigation costs no fetch.
 */

import { useEffect, useRef, useState } from "react";
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

  // Only watched while something is paused. A fetch's own result flips it
  // (null <-> path) without a navigation; fetchedFor skips those re-runs so
  // each workspace + path is read once.
  const watchedPath = suspended.length > 0 ? pathname : null;
  const fetchedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled || !currentWorkspaceId) {
      fetchedFor.current = null;
      setSuspended([]);
      return;
    }
    const key = `${currentWorkspaceId}|${pathname}`;
    if (fetchedFor.current === key) return;
    fetchedFor.current = key;
    let cancelled = false;
    let settled = false;
    getWorkspacePlan(currentWorkspaceId)
      .then((plan) => {
        settled = true;
        if (!cancelled) setSuspended(plan.suspended ?? []);
      })
      .catch(() => {
        settled = true;
        if (!cancelled) setSuspended([]);
      });
    return () => {
      cancelled = true;
      // A read dropped before it answered (e.g. StrictMode's double effect)
      // must not count as done, or the next run would skip it.
      if (!settled) fetchedFor.current = null;
    };
    // pathname is read for the key only; watchedPath carries navigation.
  }, [enabled, currentWorkspaceId, watchedPath]);

  if (onPlanPage || suspended.length === 0) return null;
  return (
    <div className="border-b border-amber-200 px-4 py-2 dark:border-amber-900">
      <SuspendedFeaturesBanner suspended={suspended} showPlanLink />
    </div>
  );
}
