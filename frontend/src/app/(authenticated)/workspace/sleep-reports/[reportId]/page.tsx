"use client";

/**
 * Workspace Sleep Report Detail Page
 *
 * Workspace owner / admin view of a single Sleep Maintenance run.
 * Issue #526.
 */

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { LoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Moon } from "lucide-react";
import Link from "next/link";
import { EmptyState } from "@/components/ui/empty-state";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { hasWorkspaceRole } from "@/lib/auth/rbac";
import { fetchWorkspaceSleepReportDetail } from "@/lib/api";
import { SleepReportDetailView } from "@/components/sleep-reports/SleepReportDetailView";
import type { SleepReportDetailResponse } from "@/lib/api/sleep-reports";

export default function WorkspaceSleepReportDetailPage() {
  const params = useParams();
  const reportId = Array.isArray(params.reportId)
    ? params.reportId[0]
    : (params.reportId ?? "");
  const t = useTranslations("workspace");
  const {
    currentWorkspace,
    currentWorkspaceId,
    loading: wsLoading,
  } = useWorkspace();

  const allowed = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    "admin",
  );

  const [detail, setDetail] = useState<SleepReportDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!currentWorkspaceId || !reportId || !allowed) return;
    const load = async () => {
      try {
        setLoading(true);
        setLoadError(null);
        setNotFound(false);
        const data = await fetchWorkspaceSleepReportDetail(
          currentWorkspaceId,
          reportId,
        );
        setDetail(data);
      } catch (error: unknown) {
        const err = error as { status?: number };
        if (err?.status === 404) {
          setNotFound(true);
        } else {
          setLoadError(t("sleepReports.messages.loadError"));
        }
      } finally {
        setLoading(false);
      }
    };
    load();
    // t is stable across renders in next-intl
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentWorkspaceId, reportId, allowed]);

  if (wsLoading) {
    return (
      <PageContainer>
        <PageHeader title={t("sleepReports.title")} />
        <LoadingState lines={5} />
      </PageContainer>
    );
  }

  if (!currentWorkspaceId) {
    return (
      <PageContainer>
        <PageHeader title={t("sleepReports.title")} />
        <ErrorBanner error={t("sleepReports.errors.noWorkspaceSelected")} />
      </PageContainer>
    );
  }

  if (!allowed) {
    return (
      <PageContainer>
        <PageHeader title={t("sleepReports.title")} />
        <ErrorBanner error={t("sleepReports.errors.forbiddenWorkspace")} />
      </PageContainer>
    );
  }

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title={t("sleepReports.title")} />
        <LoadingState lines={5} />
      </PageContainer>
    );
  }

  if (loadError) {
    return (
      <PageContainer>
        <PageHeader title={t("sleepReports.title")} />
        <ErrorBanner error={loadError} />
        <Button variant="outline" asChild className="mt-4">
          <Link href="/workspace/sleep-reports">
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("sleepReports.actions.back")}
          </Link>
        </Button>
      </PageContainer>
    );
  }

  if (notFound || !detail) {
    return (
      <PageContainer>
        <PageHeader title={t("sleepReports.title")} />
        <EmptyState
          icon={Moon}
          title={t("sleepReports.messages.notFound")}
          description=""
        />
        <Button variant="outline" asChild className="mt-4">
          <Link href="/workspace/sleep-reports">
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("sleepReports.actions.back")}
          </Link>
        </Button>
      </PageContainer>
    );
  }

  return (
    <SleepReportDetailView
      detail={detail}
      backHref="/workspace/sleep-reports"
      backLabel={t("sleepReports.actions.back")}
      translationNamespace="admin.sleepReports"
    />
  );
}
