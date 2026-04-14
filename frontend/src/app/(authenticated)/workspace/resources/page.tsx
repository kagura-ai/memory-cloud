"use client";

/**
 * Resources List Page
 *
 * Lists all resources in the current workspace with aggregated stats
 * (token count, memory count, schema version, last activity).
 *
 * Issue #47
 */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Database, ExternalLink } from "lucide-react";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { TableLoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatRelativeTime } from "@/lib/utils/datetime";
import { listResources, type ResourceListItem } from "@/lib/api/resources";
import { useWorkspace } from "@/contexts/WorkspaceContext";

export default function ResourcesListPage() {
  const router = useRouter();
  const t = useTranslations("resources");
  const { currentWorkspace } = useWorkspace();

  const [resources, setResources] = useState<ResourceListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const planName = currentWorkspace?.plan_name;
  // Wait for currentWorkspace to resolve before deciding on plan gating —
  // otherwise we flash the loading skeleton and fire a spurious API call on
  // the first render (before WorkspaceContext hydrates).
  const workspaceReady = currentWorkspace !== null && planName !== undefined;
  const isPlanGated =
    workspaceReady && (planName === "free" || planName === "basic");

  const fetchResources = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await listResources();
      setResources(res.resources);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("list.fetchError"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    // Hold until the workspace context has hydrated; avoids a free/basic user
    // issuing an authenticated round-trip before the CTA renders.
    if (!workspaceReady) return;
    if (isPlanGated) {
      setLoading(false);
      return;
    }
    fetchResources();
  }, [fetchResources, isPlanGated, workspaceReady]);

  useEffect(() => {
    document.title = `${t("list.title")} - Kagura Memory Cloud`;
  }, [t]);

  const handleRowClick = (resource: ResourceListItem) => {
    router.push(
      `/workspace/resources/${encodeURIComponent(resource.resource_id)}`,
    );
  };

  if (isPlanGated) {
    return (
      <PageContainer>
        <PageHeader
          title={t("list.title")}
          description={t("list.description")}
        />
        <EmptyState
          icon={Database}
          title={t("planGate.title")}
          description={t("planGate.description")}
          actionLabel={t("planGate.action")}
          onAction={() => router.push("/workspace/settings/billing")}
        />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader title={t("list.title")} description={t("list.description")} />

      {error && <ErrorBanner error={error} />}

      {loading ? (
        <TableLoadingState rows={5} />
      ) : resources.length === 0 ? (
        <EmptyState
          icon={Database}
          title={t("list.emptyTitle")}
          description={t("list.emptyDescription")}
        >
          <a
            href="https://github.com/kagura-ai/memory-cloud/blob/main/docs/resources.md"
            target="_blank"
            rel="noopener noreferrer"
            className="mt-4 inline-flex items-center gap-1.5 text-sm text-brand-green-600 hover:text-brand-green-700 dark:text-brand-green-400"
          >
            {t("list.setupGuide")} <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </EmptyState>
      ) : (
        <div className="rounded-lg border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("list.column.resourceId")}</TableHead>
                <TableHead>{t("list.column.context")}</TableHead>
                <TableHead className="text-right">
                  {t("list.column.tokens")}
                </TableHead>
                <TableHead className="text-right">
                  {t("list.column.memories")}
                </TableHead>
                <TableHead>{t("list.column.schema")}</TableHead>
                <TableHead>{t("list.column.lastActivity")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {resources.map((r) => (
                <TableRow
                  key={r.resource_id}
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleRowClick(r)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      handleRowClick(r);
                    }
                  }}
                >
                  <TableCell className="font-mono text-sm">
                    {r.resource_id}
                  </TableCell>
                  <TableCell>
                    {r.context_display_name || r.context_name}
                  </TableCell>
                  <TableCell className="text-right">{r.token_count}</TableCell>
                  <TableCell className="text-right">
                    {r.memory_count.toLocaleString()}
                  </TableCell>
                  <TableCell>
                    {r.current_schema_version !== null ? (
                      <Badge variant="secondary">
                        v{r.current_schema_version}
                      </Badge>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="text-muted-foreground text-sm">
                    {formatRelativeTime(r.updated_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </PageContainer>
  );
}
