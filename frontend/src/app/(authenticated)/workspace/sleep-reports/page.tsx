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
import { FeatureGateNotice } from "@/components/common/FeatureGateNotice";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useFeatureGate } from "@/hooks/useFeatureGate";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { fetchWorkspaceSleepReports } from "@/lib/api";

export default function WorkspaceSleepReportsPage() {
  const t = useTranslations("workspace");
  const { currentWorkspace, currentWorkspaceId, loading } = useWorkspace();

  // Sleep Maintenance needs a tier with `sleep_enabled_contexts_limit > 0`
  // (#1645: read from the tier matrix, not a tier rank — the server's own
  // gate through its zero floor). Mirror the resources page: keep the sidebar
  // entry, gate the page with the plan notice, whose CTA routes to the Plan
  // page (#1137) where the member may upgrade. Pending — workspace or matrix
  // still resolving — never gates.
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
    // #1646 P8: the whole page is the plan notice — the tier the matrix
    // names (none when no served tier has Sleep Maintenance), and a CTA only
    // where the gate's canUpgrade allows one.
    return (
      <FeatureGateNotice
        variant="page"
        gate={gate}
        pageTitle={t("sleepReports.title")}
        pageDescription={t("sleepReports.description")}
      />
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
