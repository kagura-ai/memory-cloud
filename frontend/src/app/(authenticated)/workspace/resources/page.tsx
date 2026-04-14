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
import Link from "next/link";
import { useTranslations, useLocale } from "next-intl";
import { useAuth } from "@/contexts/AuthContext";
import { Database } from "lucide-react";
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
  const { user } = useAuth();
  const locale = useLocale();
  const timezone = user?.timezone || "UTC";
  // Route number formatting through next-intl locale so grouping rules match
  // the selected UI language (e.g., 1,234 in en, 1,234 in ja for Latin digits
  // but proper grouping semantics per locale).
  const numberFormatter = new Intl.NumberFormat(locale);

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
      ) : error ? null : resources.length === 0 ? (
        // Empty state is reserved for "request succeeded, zero results" —
        // suppress it when the fetch errored so the ErrorBanner above isn't
        // paired with a misleading "Set one up" CTA.
        <EmptyState
          icon={Database}
          title={t("list.emptyTitle")}
          description={t("list.emptyDescription")}
        />
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
              {resources.map((r) => {
                // Navigation is a <Link> inside the first cell rather than a
                // click handler on the row itself — preserves <tr> table
                // semantics for assistive tech, and enables browser affordances
                // like middle-click / open-in-new-tab / copy-link-address.
                const href = `/workspace/resources/${encodeURIComponent(
                  r.resource_id,
                )}`;
                return (
                  <TableRow key={r.resource_id} className="hover:bg-muted/50">
                    <TableCell className="font-mono text-sm">
                      <Link
                        href={href}
                        className="hover:underline focus:outline-none focus:ring-2 focus:ring-ring rounded"
                      >
                        {r.resource_id}
                      </Link>
                    </TableCell>
                    <TableCell>
                      {r.context_display_name || r.context_name}
                    </TableCell>
                    <TableCell className="text-right">
                      {numberFormatter.format(r.token_count)}
                    </TableCell>
                    <TableCell className="text-right">
                      {numberFormatter.format(r.memory_count)}
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
                      {formatRelativeTime(r.updated_at, timezone, locale)}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </PageContainer>
  );
}
