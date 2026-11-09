"use client";

import { useTranslations } from "next-intl";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useFeatureGate } from "@/hooks/useFeatureGate";
import { PageContainer } from "@/components/common/PageContainer";
import { WorkspacePageHeader } from "@/components/dashboard/WorkspacePageHeader";
import { FeatureGateNotice } from "@/components/common/FeatureGateNotice";
import { TableLoadingState } from "@/components/common/LoadingState";
import { HistoryView } from "./HistoryView";

export default function OperationHistoryPage() {
  const t = useTranslations("operationHistory");
  const { currentWorkspace } = useWorkspace();
  const gate = useFeatureGate("operation_history");
  return (
    <PageContainer>
      <WorkspacePageHeader title={t("title")} description={t("description")} />
      {gate.state === "pending" ? <TableLoadingState rows={5} />
        : gate.state !== "allowed" ? <FeatureGateNotice gate={gate} variant="page" />
          : currentWorkspace ? <HistoryView
            key={`${currentWorkspace.id}:${currentWorkspace.current_user_role}:${currentWorkspace.plan_name}`}
            workspaceId={currentWorkspace.id}
          /> : null}
    </PageContainer>
  );
}
