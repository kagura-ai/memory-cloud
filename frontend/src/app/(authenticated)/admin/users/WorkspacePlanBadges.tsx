"use client";

/**
 * Workspace badges with the workspace's plan for the admin user list (#1754).
 *
 * Each visible badge names the workspace and nests a `PlanBadge`, so the tier
 * color carries only the plan: the workspace part is an outline badge, which
 * also keeps the gray `free` chip visible against it. The badge's `title`
 * adds the role; it is not a tab stop (a list page would otherwise gain a
 * few hundred focusable badges that do nothing on Enter).
 *
 * The third and later workspaces collapse into a "+N" button whose tooltip
 * lists them with their plans. It is a button so the list is reachable from
 * the keyboard, and a click toggles it because Radix Tooltip never opens on
 * tap.
 *
 * `plan_name` is a `string` on the wire while the DB constrains it to the four
 * canonical tiers; `PlanBadge` renders anything else as a neutral chip, so
 * the component is safe if that constraint is ever relaxed (#1645).
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
import { planLabelForTier } from "@/lib/utils/planLabel";
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

export function WorkspacePlanBadges({
  workspaces,
  maxVisible = 2,
}: WorkspacePlanBadgesProps) {
  const t = useTranslations("admin.users.table");
  const tRoles = useTranslations("admin.users.detail.roles");
  const { locale } = useLocale();

  // The "+N" tooltip is controlled. A click sets the opposite of the state at
  // pointerdown — Radix's own pointerdown handler has already closed an open
  // tooltip by the time the click fires — and preventDefault stops Radix's
  // composed onClick from closing it again (it skips a default-prevented
  // event). On touch, focus between pointerup and click can open it first;
  // the click then leaves it open. Escape, outside press and blur still
  // close it through onOpenChange.
  const [overflowOpen, setOverflowOpen] = useState(false);
  const openAtPointerDown = useRef(false);
  const toggleOverflow = (e: MouseEvent) => {
    // The table row navigates on click; nothing in here may.
    e.stopPropagation();
    e.preventDefault();
    setOverflowOpen(!openAtPointerDown.current);
    openAtPointerDown.current = false;
  };

  const describe = (workspace: WorkspaceMembership) =>
    t("workspaceTooltip", {
      name: workspace.workspace_name,
      role: tRoles.has(workspace.role)
        ? tRoles(workspace.role)
        : workspace.role,
      plan: planLabelForTier(workspace.plan_name, undefined, locale),
    });

  const visible = workspaces.slice(0, maxVisible);
  const hidden = workspaces.slice(maxVisible);

  return (
    <div className="flex flex-wrap gap-1">
      {visible.map((workspace) => (
        <Badge
          key={workspace.workspace_id}
          variant="outline"
          title={describe(workspace)}
          data-testid={WORKSPACE_BADGE_TEST_IDS.badge}
          className="gap-1 text-xs font-medium text-gray-700 dark:text-gray-200"
        >
          <Building2 className="h-3 w-3" />
          {workspace.workspace_name}
          <PlanBadge
            planName={workspace.plan_name}
            size="sm"
            className="px-1.5 py-0"
          />
        </Badge>
      ))}
      {hidden.length > 0 && (
        <TooltipProvider delayDuration={200}>
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
            <TooltipContent onClick={(e) => e.stopPropagation()}>
              <ul className="space-y-0.5">
                {hidden.map((workspace) => (
                  <li key={workspace.workspace_id}>{describe(workspace)}</li>
                ))}
              </ul>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      )}
    </div>
  );
}
