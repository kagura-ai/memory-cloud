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
import { useRouter } from "next/navigation";
import { Moon } from "lucide-react";
import { useTranslations } from "next-intl";
import {
  SleepReportsList,
  type SleepReportsListFetchParams,
} from "@/components/sleep-reports/SleepReportsList";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { fetchWorkspaceSleepReports } from "@/lib/api";

export default function WorkspaceSleepReportsPage() {
  const t = useTranslations("workspace");
  const router = useRouter();
  const { currentWorkspace, currentWorkspaceId, loading } = useWorkspace();

  // Sleep Maintenance is Pro-only (sleep_enabled_contexts_limit = 0 on
  // free/basic). Mirror the resources page: keep the sidebar entry, gate the
  // page with an upgrade CTA that routes to the Plan page (#1137).
  const planName = currentWorkspace?.plan_name;
  const isProGated = !loading && planName !== undefined && planName !== "pro";

  const allowed = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    WorkspaceRole.Admin,
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

  if (isProGated) {
    return (
      <PageContainer>
        <PageHeader
          title={t("sleepReports.title")}
          description={t("sleepReports.description")}
        />
        <EmptyState
          icon={Moon}
          title={t("sleepReports.planGate.title")}
          description={t("sleepReports.planGate.description")}
          actionLabel={t("sleepReports.planGate.action")}
          onAction={() => router.push("/workspace/settings/plan")}
        />
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
