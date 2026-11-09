"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { ChevronDown, History } from "lucide-react";
import { fromZonedTime } from "date-fns-tz";
import { useAuth } from "@/contexts/AuthContext";
import { useErrorGate } from "@/hooks/useErrorGate";
import { ApiError } from "@/lib/api/base";
import { getOperationHistory, HISTORY_OPERATIONS, type HistoryPage } from "@/lib/api/operation-history";
import { formatDateTime } from "@/lib/utils/datetime";
import { usePlanTierMatrix } from "@/hooks/usePlanFeatures";
import { requiredTierFor, resolvedPlanLabel } from "@/lib/gates/featureGates";
import { FeatureGateNotice } from "@/components/common/FeatureGateNotice";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { TableLoadingState } from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const EMPTY_FILTERS = {
  query: "", context_id: "", actor: "", agent_id: "", since: "", until: "",
  operation: "all", target_type: "all", outcome: "all",
};
type Filters = typeof EMPTY_FILTERS;

export function HistoryView({ workspaceId }: { workspaceId: string }) {
  const t = useTranslations("operationHistory");
  const locale = useLocale();
  const { user } = useAuth();
  const timezone = user?.timezone || "UTC";
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [revision, setRevision] = useState(0);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [result, setResult] = useState<{ key: string; page: HistoryPage } | null>(null);
  const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
  const cursor = cursors[cursors.length - 1];
  const requestKey = JSON.stringify([workspaceId, filters, cursor, timezone]);
  // Keep the table mounted during revalidation of the same authorized view.
  // Workspace, filter and page changes still hide previous results immediately.
  const page = result?.key === requestKey ? result.page : null;
  const error = failure?.key === requestKey ? failure.error : null;
  const errorGate = useErrorGate(error, "operation_history");
  // The lowest served tier with the entitlement, named as this deployment labels it.
  const tiers = usePlanTierMatrix();
  const minimumTier = tiers ? requiredTierFor(tiers, tier => tier.operation_history === true) : null;
  const planLabel = minimumTier ? resolvedPlanLabel(minimumTier.name, tiers, undefined, locale) : null;
  // The last served window stays shown while the same view revalidates.
  const retentionDays = result?.page.retention_days;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const params = new URLSearchParams({ limit: "30" });
    for (const [key, value] of Object.entries(filters)) {
      if (!value.trim() || value === "all") continue;
      if (key === "since" || key === "until") {
        // datetime-local is a wall-clock value in the user's saved timezone.
        // Include every instant of the selected ending minute (API is <=).
        const at = fromZonedTime(value, timezone);
        if (key === "until") at.setTime(at.getTime() + 59_999);
        params.set(key, key === "until" ? at.toISOString().replace(".999Z", ".999999Z") : at.toISOString());
      } else {
        params.set(key, value.trim());
      }
    }
    if (cursor) params.set("cursor", cursor);
    getOperationHistory(workspaceId, params, controller.signal)
      .then(data => { if (active) { setFailure(null); setResult({ key: requestKey, page: data }); } })
      .catch(err => { if (active) { setResult(null); setFailure({ key: requestKey, error: err }); } });
    return () => { active = false; controller.abort(); };
  }, [workspaceId, filters, cursor, requestKey, revision, timezone]);

  useEffect(() => {
    // Reauthorize active views after plan/role changes made in another tab.
    const refresh = () => {
      if (document.visibilityState === "visible") setRevision(value => value + 1);
    };
    const interval = window.setInterval(refresh, 30_000);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  const reset = () => { setDraft(EMPTY_FILTERS); setFilters(EMPTY_FILTERS); setCursors([null]); setRevision(v => v + 1); };
  const apply = (event: FormEvent) => {
    event.preventDefault(); setFilters({ ...draft }); setCursors([null]); setRevision(v => v + 1);
  };
  const setField = (key: keyof Filters, value: string) => setDraft(previous => ({ ...previous, [key]: value }));
  const activeFilterCount = Object.values(filters).filter(value => value.trim() && value !== "all").length;
  const mobileLabel = (key: string) => <span aria-hidden="true" className="mb-1 block text-xs font-medium text-muted-foreground xl:hidden">{t(key)}</span>;
  const select = (key: "operation" | "target_type" | "outcome", options: readonly string[], label: (value: string) => string) => (
    <div className="space-y-2" key={key}>
      <Label htmlFor={`history-${key}`}>{t(key)}</Label>
      <Select value={draft[key]} onValueChange={value => setField(key, value)}>
        <SelectTrigger id={`history-${key}`}><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="all">{t("all")}</SelectItem>
          {options.map(value => <SelectItem key={value} value={value}>{label(value)}</SelectItem>)}
        </SelectContent>
      </Select>
    </div>
  );
  return (
    <div className="space-y-6">
      {planLabel && <p className="text-sm text-muted-foreground">{t("coverage", { plan: planLabel })}</p>}
      {retentionDays !== undefined && <p className="text-sm text-muted-foreground">{t("retention", { days: retentionDays })}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" className="md:hidden" aria-expanded={filtersOpen} aria-controls="history-filters" onClick={() => setFiltersOpen(open => !open)}>
          {t("filterToggle")}{activeFilterCount > 0 && <span> ({activeFilterCount})</span>}
          <ChevronDown aria-hidden="true" className={`ml-2 h-4 w-4 ${filtersOpen ? "rotate-180" : ""}`} />
        </Button>
        <Button type="button" variant="outline" onClick={() => setRevision(v => v + 1)}>{t("refresh")}</Button>
      </div>
      <form id="history-filters" onSubmit={apply} className={`${filtersOpen ? "block" : "hidden"} space-y-4 rounded-lg border p-4 md:block`} aria-label={t("filters")}>
        {/* Two columns only where a datetime input keeps its calendar control:
            the sidebar takes 16rem from md, so md and below lg stay single. */}
        <div className="grid items-end gap-4 sm:grid-cols-2 md:grid-cols-1 lg:grid-cols-2 xl:grid-cols-3">
          {(["query", "context_id", "actor", "agent_id", "since", "until"] as const).map(key => (
            <div className="min-w-0 space-y-2" key={key}>
              <Label htmlFor={`history-${key}`}>{key === "since" || key === "until" ? t(key, { timezone }) : key === "actor" ? t("actorFilter") : t(key)}</Label>
              <Input id={`history-${key}`} value={draft[key]} onChange={e => setField(key, e.target.value)}
                className="min-w-0"
                type={key === "since" || key === "until" ? "datetime-local" : "text"}
                placeholder={key === "actor" ? t("actorPlaceholder") : undefined}
                maxLength={key === "query" ? 120 : 255}
                pattern={key === "context_id" || key === "agent_id" ? "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}" : undefined}
              />
            </div>
          ))}
          {select("operation", HISTORY_OPERATIONS, value => t(`operations.${value}`))}
          {select("target_type", ["memory", "context", "member", "credential", "integration", "sleep"], value => t(`types.${value}`))}
          {select("outcome", ["success", "failure"], value => t(`outcomes.${value}`))}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="submit">{t("apply")}</Button>
          <Button type="button" variant="outline" onClick={reset}>{t("reset")}</Button>
        </div>
      </form>
      {errorGate ? <FeatureGateNotice gate={errorGate} variant="page" />
        : error ? <ErrorBanner error={t(error instanceof ApiError && error.status === 403 ? "forbidden" : error instanceof ApiError && error.status === 401 ? "sessionExpired" : "error")} />
          : !page ? <TableLoadingState rows={5} />
            : page.items.length === 0 ? <EmptyState icon={History} title={t("empty")} description={t("emptyDescription")} actionLabel={t("reset")} onAction={reset} compact />
              : <>
                <p className="text-sm text-muted-foreground" role="status">{t(page.total_capped ? "totalCapped" : "total", { count: page.total })}</p>
                <Table role="table" aria-label={t("title")} className="block xl:table">
                  <TableHeader role="rowgroup" className="sr-only xl:not-sr-only xl:table-header-group"><TableRow role="row">{["time", "actor", "operation", "target", "outcome"].map(key => <TableHead role="columnheader" scope="col" key={key} className="xl:whitespace-nowrap">{t(key)}</TableHead>)}</TableRow></TableHeader>
                  <TableBody role="rowgroup" className="grid gap-4 xl:table-row-group [&_tr:last-child]:border xl:[&_tr:last-child]:border-0">{page.items.map(item => {
                    // A shared credential's account is not the physical caller:
                    // say so only where a credential or client is known.
                    const viaCredential = Boolean(item.credential_id || item.client_id) && item.principal_kind !== "session" && item.principal_kind !== "system";
                    return (
                    <TableRow role="row" key={item.id} className="grid min-w-0 gap-3 rounded-lg border bg-background p-4 even:bg-background dark:even:bg-background xl:table-row xl:rounded-none xl:border-0 xl:border-b xl:bg-transparent xl:p-0 xl:even:bg-slate-50/50 xl:dark:even:bg-slate-900/50">
                      <TableCell role="cell" className="block p-0 align-top xl:table-cell xl:whitespace-nowrap xl:p-2">{mobileLabel("time")}<time dateTime={item.occurred_at}>{formatDateTime(item.occurred_at, timezone, locale)}</time></TableCell>
                      <TableCell role="cell" className="block min-w-0 whitespace-normal p-0 align-top [overflow-wrap:anywhere] xl:table-cell xl:max-w-64 xl:p-2">
                        {mobileLabel("actor")}
                        <div>{item.principal_kind === "system" ? t("system")
                          : item.principal_kind === "session" ? item.actor_name || item.actor_user_id || t("unknown")
                            : item.credential_id ? t("credential", { id: item.credential_id })
                              : item.client_id ? t("client", { id: item.client_id }) : t("unknown")}</div>
                        {item.principal_kind === "session" && item.actor_name && <div className="text-xs text-muted-foreground">{item.actor_user_id}</div>}
                        {viaCredential && item.actor_user_id && <div className="text-xs text-muted-foreground">{t("principalAccount", { id: item.actor_name ? `${item.actor_name} (${item.actor_user_id})` : item.actor_user_id })}</div>}
                        {item.credential_id && item.client_id && <div className="text-xs">{t("client", { id: item.client_id })}</div>}
                        {item.agent_id && <div className="text-xs">{t("agent", { id: item.agent_id })}</div>}
                        <div className="text-xs text-muted-foreground">{t(`surfaces.${item.surface}`)}</div>
                        {viaCredential && <div className="text-xs text-muted-foreground">{t("accountAttribution")}</div>}
                      </TableCell>
                      <TableCell role="cell" className="block min-w-0 p-0 align-top xl:table-cell xl:p-2">
                        {mobileLabel("operation")}
                        {t(`operations.${item.operation}`)}
                        {item.count > 1 && <div className="text-xs">{t("count", { count: item.count })}</div>}
                        {item.changed_fields.length > 0 && <div className="max-w-52 break-words text-xs text-muted-foreground">{item.changed_fields.map(field => t(`fields.${field}`)).join(", ")}</div>}
                      </TableCell>
                      <TableCell role="cell" className="block min-w-0 whitespace-normal p-0 align-top [overflow-wrap:anywhere] xl:table-cell xl:max-w-64 xl:p-2">
                        {mobileLabel("target")}
                        {/* A membership event's target IS the affected member: name it once. */}
                        <div>{item.subject_user_id && item.subject_user_id === item.target_id ? t("subject", { id: item.target_id }) : item.target_id || t("multiple")}</div>
                        {item.subject_user_id && item.subject_user_id !== item.target_id && <div className="text-xs">{t("subject", { id: item.subject_user_id })}</div>}
                        {item.context_id && <div className="text-xs text-muted-foreground">{t("context", { id: item.context_id })}</div>}
                        {item.target_type === "memory" && item.context_id && item.target_id && item.count === 1 && <Link className="text-sm underline" href={`/workspace/contexts/${encodeURIComponent(item.context_id)}?tab=memories&memoryId=${encodeURIComponent(item.target_id)}`}>{t("details")}</Link>}
                        {item.target_type === "sleep" && item.target_id && <Link className="text-sm underline" href={`/workspace/sleep-reports/${encodeURIComponent(item.target_id)}`}>{t("details")}</Link>}
                      </TableCell>
                      <TableCell role="cell" className="block p-0 align-top xl:table-cell xl:whitespace-nowrap xl:p-2">{mobileLabel("outcome")}{t(`outcomes.${item.outcome}`)}</TableCell>
                    </TableRow>
                    );
                  })}</TableBody>
                </Table>
              </>}
      <div className="flex items-center gap-3">
        <Button variant="outline" disabled={cursors.length === 1 || !page} onClick={() => setCursors(values => values.slice(0, -1))}>{t("previous")}</Button>
        <span className="text-sm">{t("page", { page: cursors.length })}</span>
        <Button variant="outline" disabled={!page?.next_cursor} onClick={() => { if (page?.next_cursor) setCursors(values => [...values, page.next_cursor]); }}>{t("next")}</Button>
      </div>
    </div>
  );
}
