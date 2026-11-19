"use client";

/**
 * The workspaces the signed-in user owns (#2919): name, its own plan, status,
 * and what can be done with each.
 */

import { useTranslations } from "next-intl";
import { Download, Play, Trash2 } from "lucide-react";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { InlineSpinner } from "@/components/common/LoadingState";
import { PlanBadge } from "@/components/common/PlanBadge";
import { useAuth } from "@/contexts/AuthContext";
import { useLocale } from "@/i18n";
import { formatDate } from "@/lib/utils/datetime";
import type { SlotWorkspace } from "@/lib/api/workspaceSlots";

export interface OwnedWorkspacesTableProps {
  workspaces: SlotWorkspace[];
  /** Plan changes are served on this deployment (`features.plan_page`). */
  planPage: boolean;
  /** The workspace whose plan page is opening, if any. */
  openingPlanFor: string | null;
  onChangePlan: (workspace: SlotWorkspace) => void;
  onExport: (workspace: SlotWorkspace) => void;
  onDelete: (workspace: SlotWorkspace) => void;
  onResume: (workspace: SlotWorkspace) => void;
}

export function OwnedWorkspacesTable({
  workspaces,
  planPage,
  openingPlanFor,
  onChangePlan,
  onExport,
  onDelete,
  onResume,
}: OwnedWorkspacesTableProps) {
  const t = useTranslations("workspaceSlots");
  const { user } = useAuth();
  const { locale } = useLocale();
  const timezone = user?.timezone || "UTC";

  return (
    <Table>
      <TableCaption className="sr-only">{t("table.caption")}</TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead>{t("table.name")}</TableHead>
          <TableHead>{t("table.plan")}</TableHead>
          <TableHead>{t("table.status")}</TableHead>
          <TableHead>{t("table.actions")}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {workspaces.map((w) => {
          const suspended = w.status === "slot_suspended";
          const opening = openingPlanFor === w.id;
          return (
            <TableRow key={w.id} data-testid={`slot-row-${w.id}`}>
              <TableCell className="align-top">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="break-words font-medium">{w.name}</span>
                  {w.is_current && (
                    <Badge variant="secondary">{t("table.current")}</Badge>
                  )}
                </div>
              </TableCell>
              <TableCell className="align-top">
                <div className="flex flex-col items-start gap-1">
                  <PlanBadge planName={w.plan_name} size="sm" />
                  {planPage && (
                    <Button
                      type="button"
                      variant="link"
                      size="sm"
                      className="h-auto p-0"
                      onClick={() => onChangePlan(w)}
                      disabled={openingPlanFor !== null}
                      aria-busy={opening}
                    >
                      {opening && <InlineSpinner size="sm" className="mr-1" />}
                      {t("changePlan")}
                    </Button>
                  )}
                </div>
              </TableCell>
              <TableCell className="max-w-sm align-top">
                {suspended ? (
                  <div className="space-y-1">
                    <Badge
                      variant="outline"
                      className="border-amber-400 text-amber-900 dark:border-amber-500 dark:text-amber-100"
                    >
                      {t("status.slot_suspended")}
                    </Badge>
                    {w.suspended_at && (
                      <p className="text-xs text-muted-foreground">
                        {t("status.suspendedOn", {
                          date: formatDate(w.suspended_at, timezone, locale),
                        })}
                      </p>
                    )}
                    <p className="text-xs text-muted-foreground">{t("reason")}</p>
                  </div>
                ) : (
                  <Badge
                    variant="outline"
                    className="border-emerald-500 text-emerald-900 dark:border-emerald-500 dark:text-emerald-100"
                  >
                    {t("status.active")}
                  </Badge>
                )}
              </TableCell>
              <TableCell className="align-top">
                {suspended ? (
                  <div className="flex flex-wrap gap-2">
                    <Button type="button" size="sm" onClick={() => onResume(w)}>
                      <Play className="mr-1 h-4 w-4" aria-hidden="true" />
                      {t("actions.resume")}
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => onExport(w)}
                    >
                      <Download className="mr-1 h-4 w-4" aria-hidden="true" />
                      {t("actions.export")}
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      className="text-red-700 dark:text-red-300"
                      onClick={() => onDelete(w)}
                    >
                      <Trash2 className="mr-1 h-4 w-4" aria-hidden="true" />
                      {t("actions.delete")}
                    </Button>
                  </div>
                ) : w.under_billing_contract ? (
                  <p className="text-xs text-muted-foreground">{t("contractNote")}</p>
                ) : null}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
