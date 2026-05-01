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
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import {
  CostDashboard,
  type CostDashboardFetchParams,
} from "@/components/cost/CostDashboard";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { hasWorkspaceRole } from "@/lib/auth/rbac";
import { fetchWorkspaceCostAggregation } from "@/lib/api";

export default function WorkspaceCostPage() {
  const t = useTranslations("admin.cost");
  const { currentWorkspace, currentWorkspaceId, loading } = useWorkspace();

  const allowed = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    "admin",
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
