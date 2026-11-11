"use client";

/**
 * Plan change history (the Quotas page's Audit tab, moved into the admin
 * Users page's Plan changes category in #2765). The last 100 changes, newest
 * first, each actor linked to their admin user detail.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { History } from "lucide-react";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { PlanBadge } from "@/components/common/PlanBadge";
import { TableLoadingState } from "@/components/common/LoadingState";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  adminUserPath,
  getAdminPlanAudit,
  type PlanChangeAuditEntry,
} from "@/lib/api/admin";
import type { PlanTier } from "@/lib/utils/planLabel";

/** As many changes as the Quotas page's Audit tab listed. */
const AUDIT_LIMIT = 100;

export function PlanAuditTable() {
  const t = useTranslations("admin.plans");
  const tCommon = useTranslations("admin.common");
  const [auditLog, setAuditLog] = useState<PlanChangeAuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  // A flag, not the message: the fetch must not depend on the translator.
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    getAdminPlanAudit(AUDIT_LIMIT)
      .then((data) => {
        if (active) setAuditLog(data);
      })
      .catch(() => {
        if (active) setFailed(true);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("auditTable.title")}</CardTitle>
        <CardDescription>{t("auditTable.description")}</CardDescription>
      </CardHeader>
      <CardContent>
        <ErrorBanner error={failed ? t("messages.loadError") : null} />
        {loading ? (
          <TableLoadingState rows={5} />
        ) : failed ? null : auditLog.length === 0 ? (
          <EmptyState
            icon={History}
            title={tCommon("noChanges")}
            description={t("auditTable.emptyDescription")}
            compact
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("auditTable.date")}</TableHead>
                <TableHead>{t("auditTable.workspace")}</TableHead>
                <TableHead>{t("auditTable.change")}</TableHead>
                <TableHead>{t("auditTable.changedBy")}</TableHead>
                <TableHead>{t("auditTable.reason")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {auditLog.map((entry) => (
                <TableRow key={entry.id}>
                  <TableCell>
                    {new Date(entry.changed_at).toLocaleString()}
                  </TableCell>
                  <TableCell>{entry.workspace_name}</TableCell>
                  <TableCell>
                    {entry.old_plan && (
                      <>
                        <PlanBadge
                          planName={entry.old_plan as PlanTier}
                          size="sm"
                        />
                        <span className="mx-2">→</span>
                      </>
                    )}
                    <PlanBadge
                      planName={entry.new_plan as PlanTier}
                      size="sm"
                    />
                  </TableCell>
                  <TableCell>
                    {entry.changed_by_email ? (
                      <Link
                        href={adminUserPath(entry.changed_by)}
                        className="block hover:underline"
                      >
                        {entry.changed_by_name && (
                          <span className="block text-sm font-medium">
                            {entry.changed_by_name}
                          </span>
                        )}
                        <span className="block text-xs text-gray-500">
                          {entry.changed_by_email}
                        </span>
                      </Link>
                    ) : (
                      entry.changed_by
                    )}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {entry.reason || "-"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
