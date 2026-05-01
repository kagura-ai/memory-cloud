"use client";

/**
 * Admin Cost Aggregation Dashboard (Issue #473).
 *
 * Cross-workspace view: admin-only. Renders the shared
 * ``CostDashboard`` against the admin endpoint, which returns one row
 * per (period × workspace × user × model × source × paid_by) — hence
 * the workspace column is shown for cross-workspace comparison.
 *
 * Workspace owner / admin self-service is at ``/workspace/cost`` and
 * uses the workspace-scoped endpoint shipped in #472.
 */

import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { CostDashboard } from "@/components/cost/CostDashboard";
import { useAuth } from "@/contexts/AuthContext";
import { isAdmin } from "@/lib/auth/rbac";
import { fetchAdminCostAggregation } from "@/lib/api";

export default function AdminCostPage() {
  const t = useTranslations("admin.cost");
  const { user } = useAuth();

  if (!isAdmin(user)) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <ErrorBanner error={t("errors.forbidden")} />
      </PageContainer>
    );
  }

  // Stable module-level reference passed directly — no closure, no
  // useCallback needed. Keeps CostDashboard's effect dep set quiet.
  return (
    <CostDashboard
      title={t("title")}
      description={t("description")}
      fetchData={fetchAdminCostAggregation}
      showWorkspaceColumn
    />
  );
}
