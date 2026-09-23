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
import { useCanUpgrade } from "@/hooks/useCanUpgrade";
import { useFeatureGate } from "@/hooks/useFeatureGate";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { fetchWorkspaceSleepReports } from "@/lib/api";

export default function WorkspaceSleepReportsPage() {
  const t = useTranslations("workspace");
  const router = useRouter();
  const { currentWorkspace, currentWorkspaceId, loading } = useWorkspace();
  // #1643: called here, above every conditional return, because hooks may not
  // sit below one. The plan-gate copy below renders either way.
  const canUpgrade = useCanUpgrade();

  // Sleep Maintenance needs a tier with `sleep_enabled_contexts_limit > 0`
  // (#1645: read from the tier matrix, not a tier rank — the server's own
  // gate through its zero floor). Mirror the resources page: keep the sidebar
  // entry, gate the page with an upgrade CTA that routes to the Plan page
  // (#1137). Pending — workspace or matrix still resolving — never gates.
  // The gate is admin-minimum and answers the role first, so a member on a
  // low tier falls through to the role branch below, never the upsell.
  const gate = useFeatureGate("sleep_reports");

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

  if (gate.state === "plan") {
    // The tier the matrix names, ready for copy that names one (#1646); none
    // when no served tier has Sleep Maintenance.
    const planValues =
      gate.planLabel !== undefined ? { plan: gate.planLabel } : undefined;
    return (
      <PageContainer>
        <PageHeader
          title={t("sleepReports.title")}
          description={t("sleepReports.description")}
        />
        {/* #1643: EmptyState renders its Button only when both actionLabel
            and onAction are set, so withholding them is how "no action" is
            expressed here. The title and description always render. */}
        <EmptyState
          icon={Moon}
          title={t("sleepReports.planGate.title", planValues)}
          description={t("sleepReports.planGate.description", planValues)}
          {...(canUpgrade === true
            ? {
                actionLabel: t("sleepReports.planGate.action"),
                onAction: () => router.push("/workspace/settings/plan"),
              }
            : {})}
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
      // #1645: not before the plan answer is known either — a pending gate
      // shows the list's skeleton, never gated reports or a refused fetch.
      ready={!!currentWorkspaceId && allowed && gate.state === "allowed"}
    />
  );
}
