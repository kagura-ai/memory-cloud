"use client";

/**
 * Workspace-scoped Cost Aggregation Dashboard (Issue #473).
 *
 * Workspace owner / admin self-service view. Renders the shared
 * ``CostDashboard`` against the workspace endpoint shipped in #472.
 *
 * Auth gates layered defense-in-depth:
 * - Sidebar nav hides this entry below ``admin`` workspace role
 * - This page checks ``hasWorkspaceRole(..., "admin")`` and renders an
 *   ``ErrorBanner`` for member/viewer (paranoid — they can't reach the
 *   nav, but a direct URL hit still gets a friendly message instead of
 *   a raw 403 from the backend)
 * - Backend ``check_workspace_admin`` is the source of truth and would
 *   reject viewer/member regardless
 *
 * The shared ``CostDashboard`` accepts a ``ready`` prop so the initial
 * fetch waits until ``currentWorkspaceId`` resolves — without this the
 * first render would request ``/api/v1/workspaces//cost-aggregation``
 * (empty path segment) and 404.
 */

import { useCallback } from "react";
import { useTranslations } from "next-intl";
import { FeatureGateNotice } from "@/components/common/FeatureGateNotice";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { SpinnerLoading } from "@/components/common/LoadingState";
import {
  CostDashboard,
  type CostDashboardFetchParams,
} from "@/components/cost/CostDashboard";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useFeatureGate } from "@/hooks/useFeatureGate";
import { isBlocked } from "@/lib/gates/featureGates";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { fetchWorkspaceCostAggregation } from "@/lib/api";

export default function WorkspaceCostPage() {
  const t = useTranslations("admin.cost");
  const tCommon = useTranslations("common");
  const { currentWorkspace, currentWorkspaceId, loading } = useWorkspace();
  // Issue #1167: gated behind the backend ENABLE_BYOK flag (like the plan
  // page #1145) — the workspace cost API returns 404 when BYOK is off.
  // Issue #1571: and behind ENABLE_COST_DISPLAY (a flat-price hosted
  // deployment hides money from workspace users — the API 404s, the nav
  // entry is hidden). #1646 D1/D2: both flags are the one `cost_dashboard`
  // gate — `pending` until /system/info resolves, `deployment` when either
  // is off (fail closed).
  const gate = useFeatureGate("cost_dashboard");

  const allowed = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    WorkspaceRole.Admin,
  );

  // useCallback keyed on currentWorkspaceId only. Without this, every
  // WorkspaceProvider re-render (workspace switch settling, auth context
  // propagation, plain parent re-renders) would produce a new closure
  // identity → CostDashboard's loadCost recompiles → useEffect re-fires
  // → an extra /cost-aggregation GET per re-render. The stable
  // reference scopes the refetch to actual workspace switches.
  const fetchData = useCallback(
    (params: CostDashboardFetchParams) =>
      fetchWorkspaceCostAggregation(currentWorkspaceId ?? "", params),
    [currentWorkspaceId],
  );

  // Wait for the feature flags, then render the deployment notice when the
  // dashboard is off here (the sidebar entry is hidden too, so this only
  // fires on direct navigation). One notice for both flags (C-19); a
  // deployment gate never carries an upgrade CTA.
  if (gate.state === "pending") {
    return <SpinnerLoading size="lg" message={tCommon("loading")} />;
  }
  if (isBlocked(gate)) {
    return (
      <FeatureGateNotice variant="page" gate={gate} pageTitle={t("title")} />
    );
  }

  // Distinguish "no workspace exists / selected" from "wrong role".
  // Without this branch a brand-new account with zero workspaces would
  // see a misleading "owner/admin role required" banner; the real
  // issue is that there's no workspace to be the owner OF.
  if (!loading && !currentWorkspaceId) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <ErrorBanner error={t("errors.noWorkspaceSelected")} />
      </PageContainer>
    );
  }

  if (!loading && !allowed) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <ErrorBanner error={t("errors.forbiddenWorkspace")} />
      </PageContainer>
    );
  }

  return (
    <CostDashboard
      title={t("title")}
      description={t("descriptionWorkspace")}
      fetchData={fetchData}
      showWorkspaceColumn={false}
      ready={!!currentWorkspaceId}
    />
  );
}
