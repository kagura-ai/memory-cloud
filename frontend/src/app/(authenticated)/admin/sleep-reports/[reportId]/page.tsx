"use client";

/**
 * Admin Sleep Report Detail Page
 *
 * View a single Sleep Maintenance run with phase summaries and action log.
 * Admin-only page (Issue #179).
 *
 * Issue #526: Refactored to use shared ``SleepReportDetailView`` component.
 */

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { LoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { Button } from "@/components/ui/button";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { fetchAdminSleepReportDetail } from "@/lib/api/sleep-reports";
import { SleepReportDetailView } from "@/components/sleep-reports/SleepReportDetailView";
import type { SleepReportDetailResponse } from "@/lib/api/sleep-reports";

export default function AdminSleepReportDetailPage() {
  const params = useParams();
  const reportId = Array.isArray(params.reportId)
    ? params.reportId[0]
    : (params.reportId ?? "");
  const t = useTranslations("admin.sleepReports");

  const [detail, setDetail] = useState<SleepReportDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!reportId) return;
    const load = async () => {
      try {
        setLoading(true);
        setLoadError(null);
        setNotFound(false);
        const data = await fetchAdminSleepReportDetail(reportId);
        setDetail(data);
      } catch (error: unknown) {
        const err = error as { status?: number };
        if (err?.status === 404) {
          setNotFound(true);
        } else {
          setLoadError(t("messages.loadError"));
        }
      } finally {
        setLoading(false);
      }
    };
    load();
    // t is stable across renders in next-intl
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reportId]);

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <LoadingState lines={5} />
      </PageContainer>
    );
  }

  if (loadError) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <ErrorBanner error={loadError} />
        <Link href="/admin/sleep-reports">
          <Button variant="outline">
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("actions.back")}
          </Button>
        </Link>
      </PageContainer>
    );
  }

  if (notFound || !detail) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <div className="p-8 text-center text-gray-500 dark:text-gray-400">
          {t("messages.notFound")}
        </div>
        <Link href="/admin/sleep-reports">
          <Button variant="outline">
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("actions.back")}
          </Button>
        </Link>
      </PageContainer>
    );
  }

  return (
    <SleepReportDetailView
      detail={detail}
      backHref="/admin/sleep-reports"
      backLabel={t("actions.back")}
    />
  );
}
