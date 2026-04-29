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
import { ChevronRight, Database } from "lucide-react";
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
  const locale = useLocale();
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
    // Issue #389: Owner-only access. Non-owner roles (admin / member /
    // viewer) never hit the API call below — silent redirect matches the
    // UX used by #365 (settings/general) and #381 (external-keys).
    if (currentWorkspace && currentWorkspace.current_user_role !== "owner") {
      router.push("/workspace/dashboard");
      return;
    }
    if (isPlanGated) {
      setLoading(false);
      return;
    }
    fetchResources();
    // router from next/navigation is stable and is intentionally excluded
    // from the dependency array; watching currentWorkspace?.current_user_role
    // as a scalar avoids re-running on every object-ref churn from
    // WorkspaceContext's selector.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    fetchResources,
    isPlanGated,
    workspaceReady,
    currentWorkspace?.current_user_role,
  ]);

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
                {/* Trailing chevron column — pure navigation affordance,
                    aria-label only since the icon is decorative for sighted
                    users. The Link in the first cell carries the real
                    accessible label for keyboard / screen-reader users. */}
                <TableHead className="w-8" aria-label={t("list.column.open")} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {resources.map((r) => {
                // Navigation strategy:
                //   - The <Link> in the first cell is the canonical, a11y-
                //     primary affordance — it carries the keyboard tab stop,
                //     screen-reader semantics, middle-click open-in-new-tab,
                //     copy-link-address, etc.
                //   - The whole-row onClick + cursor-pointer is the *added*
                //     UX: stronger discoverability so the operator does not
                //     have to know to click the slug. We bail out when the
                //     click target is itself an <a>/<button> (the slug Link)
                //     so the browser's native handler (modifiers, middle-
                //     click) is not double-fired and so interactive children
                //     keep their own behavior.
                //   - The trailing chevron is a visual hint only — the click
                //     target is the row.
                const href = `/workspace/resources/${encodeURIComponent(
                  r.resource_id,
                )}`;
                const handleRowClick = (
                  ev: React.MouseEvent<HTMLTableRowElement>,
                ) => {
                  // Mirror the browser's native <a> click semantics so the
                  // row-level navigation never overrides what the operator
                  // already expects from a link: modifier-clicks open in a
                  // new tab/window, secondary-button clicks open context
                  // menus, etc. Without these guards Cmd/Ctrl/Shift-clicking
                  // the row would still router.push() in the same tab,
                  // surprising muscle memory built up from the slug Link.
                  if (
                    ev.button !== 0 ||
                    ev.metaKey ||
                    ev.ctrlKey ||
                    ev.shiftKey ||
                    ev.altKey
                  ) {
                    return;
                  }
                  const target = ev.target as HTMLElement;
                  if (target.closest("a, button")) return;
                  router.push(href);
                };
                return (
                  <TableRow
                    key={r.resource_id}
                    className="cursor-pointer hover:bg-muted/50"
                    onClick={handleRowClick}
                  >
                    <TableCell className="font-mono text-sm">
                      <Link
                        href={href}
                        className="text-primary focus:outline-none focus:ring-2 focus:ring-ring rounded"
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
                      {formatRelativeTime(r.updated_at, locale)}
                    </TableCell>
                    <TableCell className="w-8 text-muted-foreground">
                      <ChevronRight className="h-4 w-4" aria-hidden="true" />
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
