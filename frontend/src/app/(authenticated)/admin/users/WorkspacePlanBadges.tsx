"use client";

/**
 * Workspace badges with the workspace's plan for the admin user list (#1754).
 *
 * Each visible badge names the workspace and nests a `PlanBadge`, so the tier
 * color carries only the plan: the workspace part is an outline badge, which
 * also keeps the gray `free` chip visible against it. The third and later
 * workspaces collapse into a "+N" button whose tooltip lists them with their
 * plans — a button, not a styled div, so the list is reachable from the
 * keyboard, and a click toggles it because Radix Tooltip does not open on
 * tap. Every badge is a tooltip trigger as well, replacing the native `title`
 * the badges used before.
 *
 * `plan_name` arrives as a plain string: `/plans/tiers` can serve
 * operator-defined tiers the client's `PlanTier` union has never heard of
 * (#1645), so an unknown tier renders its raw name in a neutral chip instead
 * of an unstyled `PlanBadge`.
 */

import { useRef, useState, type MouseEvent } from "react";
import { useTranslations } from "next-intl";
import { Building2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { PlanBadge } from "@/components/common/PlanBadge";
import { useLocale } from "@/i18n";
import { isPlanTier, planLabelForTier } from "@/lib/utils/planLabel";
import { WORKSPACE_BADGE_TEST_IDS } from "./testids";

export interface WorkspaceMembership {
  workspace_id: string;
  workspace_name: string;
  role: string;
  plan_name: string;
}

interface WorkspacePlanBadgesProps {
  workspaces: WorkspaceMembership[];
  /** Badges shown before the rest collapse into "+N". */
  maxVisible?: number;
}

/** The table row navigates on click; nothing inside the badges may. */
const stopClick = (e: MouseEvent) => e.stopPropagation();

function PlanChip({ planName, label }: { planName: string; label: string }) {
  if (isPlanTier(planName)) {
    return <PlanBadge planName={planName} size="sm" className="px-1.5 py-0" />;
  }
  return (
    <Badge className="bg-gray-100 px-1.5 py-0 text-xs font-semibold text-gray-700 dark:bg-gray-800 dark:text-gray-200">
      {label}
    </Badge>
  );
}

export function WorkspacePlanBadges({
  workspaces,
  maxVisible = 2,
}: WorkspacePlanBadgesProps) {
  const t = useTranslations("admin.users.table");
  const tRoles = useTranslations("admin.users.detail.roles");
  const { locale } = useLocale();
  // Radix Tooltip never opens on tap, so the "+N" list would be unreachable
  // on touch. The state is controlled and a click sets it to the opposite of
  // what it was at pointerdown: Radix's own pointerdown handler has already
  // closed an open tooltip by the time the click fires, so reading the state
  // then would reopen it. Outside press, Escape and blur still close it
  // through onOpenChange as usual.
  const [overflowOpen, setOverflowOpen] = useState(false);
  const openAtPointerDown = useRef(false);
  const toggleOverflow = (e: MouseEvent) => {
    stopClick(e);
    setOverflowOpen(!openAtPointerDown.current);
    openAtPointerDown.current = false;
  };

  if (workspaces.length === 0) return null;

  // Deployment display names, then S/M/L/XL, then the raw tier (#1645).
  const planLabel = (planName: string) =>
    planLabelForTier(planName, undefined, locale);
  const roleLabel = (role: string) => (tRoles.has(role) ? tRoles(role) : role);
  const describe = (workspace: WorkspaceMembership) =>
    t("workspaceTooltip", {
      name: workspace.workspace_name,
      role: roleLabel(workspace.role),
      plan: planLabel(workspace.plan_name),
    });

  const visible = workspaces.slice(0, maxVisible);
  const hidden = workspaces.slice(maxVisible);

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex flex-wrap gap-1">
        {visible.map((workspace) => (
          <Tooltip key={workspace.workspace_id}>
            <TooltipTrigger asChild>
              <Badge
                variant="outline"
                tabIndex={0}
                data-testid={WORKSPACE_BADGE_TEST_IDS.badge}
                className="gap-1 text-xs font-medium text-gray-700 dark:text-gray-200"
              >
                <Building2 className="h-3 w-3" />
                {workspace.workspace_name}
                <PlanChip
                  planName={workspace.plan_name}
                  label={planLabel(workspace.plan_name)}
                />
              </Badge>
            </TooltipTrigger>
            <TooltipContent onClick={stopClick}>
              {describe(workspace)}
            </TooltipContent>
          </Tooltip>
        ))}
        {hidden.length > 0 && (
          <Tooltip open={overflowOpen} onOpenChange={setOverflowOpen}>
            <TooltipTrigger asChild>
              <button
                type="button"
                data-testid={WORKSPACE_BADGE_TEST_IDS.overflow}
                aria-label={t("moreWorkspaces", { count: hidden.length })}
                onPointerDown={() => {
                  openAtPointerDown.current = overflowOpen;
                }}
                onClick={toggleOverflow}
                className="inline-flex items-center rounded-md border border-transparent bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-600 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 dark:bg-gray-700 dark:text-gray-300"
              >
                +{hidden.length}
              </button>
            </TooltipTrigger>
            <TooltipContent onClick={stopClick}>
              <ul className="space-y-0.5">
                {hidden.map((workspace) => (
                  <li key={workspace.workspace_id}>{describe(workspace)}</li>
                ))}
              </ul>
            </TooltipContent>
          </Tooltip>
        )}
      </div>
    </TooltipProvider>
  );
}
