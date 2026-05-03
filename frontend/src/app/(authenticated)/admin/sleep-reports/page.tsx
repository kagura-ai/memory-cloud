"use client";

/**
 * Admin Sleep Reports List Page
 *
 * View Sleep Maintenance execution history across all workspaces.
 * Admin-only page (Issue #179).
 *
 * Issue #526: Refactored to use shared ``SleepReportsList`` component.
 */

import { useCallback, useState } from "react";
import { useTranslations } from "next-intl";
import { useAuth } from "@/contexts/AuthContext";
import { apiClient, ApiError } from "@/lib/api";
import {
  SleepReportsList,
  type SleepReportsListFetchParams,
} from "@/components/sleep-reports/SleepReportsList";
import { useToast } from "@/hooks/use-toast";
import { fetchAdminSleepReports } from "@/lib/api/sleep-reports";
import type { SleepRunResponse } from "@/lib/api/sleep-reports";

export default function AdminSleepReportsPage() {
  const t = useTranslations("admin.sleepReports");
  const tCommon = useTranslations("admin.common");
  const { user } = useAuth();
  const { toast } = useToast();
  const [running, setRunning] = useState(false);

  const fetchData = useCallback(
    async (params: SleepReportsListFetchParams) =>
      fetchAdminSleepReports(params),
    [],
  );

  const handleRunNow = useCallback(async () => {
    try {
      setRunning(true);
      await apiClient.post<SleepRunResponse>("/api/v1/admin/sleep/run", {
        context_id: null,
      });
      toast({
        title: t("messages.runStarted"),
        description: t("messages.runStartedDesc"),
      });
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null;
      if (apiErr?.status === 409) {
        const runningReportId = apiErr.details?.running_report_id as
          | string
          | undefined;
        toast({
          title: t("messages.runConflict"),
          description: runningReportId
            ? t("messages.runConflictViewLink")
            : undefined,
          variant: "destructive",
        });
      } else {
        toast({
          title: tCommon("error"),
          description:
            err instanceof Error ? err.message : t("messages.runError"),
          variant: "destructive",
        });
      }
    } finally {
      setRunning(false);
    }
  }, [t, tCommon, toast]);

  return (
    <SleepReportsList
      title={t("title")}
      description={t("description")}
      fetchData={fetchData}
      detailHrefPrefix="/admin/sleep-reports"
      showRunNow={user?.role === "admin"}
      onRunNow={handleRunNow}
      running={running}
    />
  );
}
