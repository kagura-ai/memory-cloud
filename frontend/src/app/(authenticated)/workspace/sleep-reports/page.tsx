"use client";

/**
 * Workspace Sleep Reports List Page
 *
 * Workspace owner / admin self-service view of Sleep Maintenance
 * reports for their workspace only (Issue #526).
 *
 * Mirrors ``/workspace/cost`` pattern from #473.
 */

import { useCallback } from "react";
import { useTranslations } from "next-intl";
import {
  SleepReportsList,
  type SleepReportsListFetchParams,
} from "@/components/sleep-reports/SleepReportsList";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { hasWorkspaceRole } from "@/lib/auth/rbac";
import { fetchWorkspaceSleepReports } from "@/lib/api";

export default function WorkspaceSleepReportsPage() {
  const t = useTranslations("workspace");
  const { currentWorkspace, currentWorkspaceId, loading } = useWorkspace();

  const allowed = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    "admin",
  );

  const fetchData = useCallback(
    (params: SleepReportsListFetchParams) =>
      fetchWorkspaceSleepReports(currentWorkspaceId ?? "", params),
    [currentWorkspaceId],
  );

  if (!loading && !currentWorkspaceId) {
    return (
      <PageContainer>
        <PageHeader title={t("sleepReports.title")} />
        <ErrorBanner error={t("sleepReports.errors.noWorkspaceSelected")} />
      </PageContainer>
    );
  }

  if (!loading && !allowed) {
    return (
      <PageContainer>
        <PageHeader title={t("sleepReports.title")} />
        <ErrorBanner error={t("sleepReports.errors.forbiddenWorkspace")} />
      </PageContainer>
    );
  }

  return (
    <SleepReportsList
      title={t("sleepReports.title")}
      description={t("sleepReports.description")}
      fetchData={fetchData}
      detailHrefPrefix="/workspace/sleep-reports"
      translationNamespace="admin.sleepReports"
      ready={!!currentWorkspaceId && allowed}
    />
  );
}
