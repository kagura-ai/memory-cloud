"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { History } from "lucide-react";
import { useErrorGate } from "@/hooks/useErrorGate";
import { getOperationHistory, HISTORY_OPERATIONS, type HistoryPage } from "@/lib/api/operation-history";
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
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<{ key: string; page: HistoryPage } | null>(null);
  const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
  const cursor = cursors[cursors.length - 1];
  const requestKey = JSON.stringify([workspaceId, filters, cursor, revision]);
  // Results and errors belong to one request. A filter/workspace change never
  // renders the previous request's data, even before effect cleanup runs.
  const page = result?.key === requestKey ? result.page : null;
  const error = failure?.key === requestKey ? failure.error : null;
  const errorGate = useErrorGate(error, "operation_history");

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const params = new URLSearchParams({ limit: "30" });
    for (const [key, value] of Object.entries(filters)) {
      if (!value || value === "all") continue;
      params.set(key, key === "since" || key === "until" ? new Date(value).toISOString() : value.trim());
    }
    if (cursor) params.set("cursor", cursor);
    getOperationHistory(workspaceId, params, controller.signal)
      .then(data => { if (active) { setFailure(null); setResult({ key: requestKey, page: data }); } })
      .catch(err => { if (active) { setResult(null); setFailure({ key: requestKey, error: err }); } });
    return () => { active = false; controller.abort(); };
  }, [workspaceId, filters, cursor, requestKey]);

  useEffect(() => {
    // Reauthorize active views after plan/role changes made in another tab.
    const refresh = () => setRevision(value => value + 1);
    const interval = window.setInterval(refresh, 30_000);
    window.addEventListener("focus", refresh);
    return () => { window.clearInterval(interval); window.removeEventListener("focus", refresh); };
  }, []);

  const reset = () => { setDraft(EMPTY_FILTERS); setFilters(EMPTY_FILTERS); setCursors([null]); setRevision(v => v + 1); };
  const apply = (event: FormEvent) => {
    event.preventDefault(); setFilters({ ...draft }); setCursors([null]); setRevision(v => v + 1);
  };
  const setField = (key: keyof Filters, value: string) => setDraft(previous => ({ ...previous, [key]: value }));
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
      <p className="text-sm text-muted-foreground">{t("coverage")}</p>
      <form onSubmit={apply} className="space-y-4 rounded-lg border p-4" aria-label={t("filters")}>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {(["query", "context_id", "actor", "agent_id", "since", "until"] as const).map(key => (
            <div className="space-y-2" key={key}>
              <Label htmlFor={`history-${key}`}>{t(key)}</Label>
              <Input id={`history-${key}`} value={draft[key]} onChange={e => setField(key, e.target.value)}
                type={key === "since" || key === "until" ? "datetime-local" : "text"}
                maxLength={key === "query" ? 120 : 255}
                pattern={key === "context_id" || key === "agent_id" ? "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}" : undefined}
              />
            </div>
          ))}
          {select("operation", HISTORY_OPERATIONS, value => t(`operations.${value}`))}
          {select("target_type", ["memory", "context", "member", "credential", "integration", "sleep"], value => t(`types.${value}`))}
          {select("outcome", ["success", "failure", "denied"], value => t(`outcomes.${value}`))}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="submit">{t("apply")}</Button>
          <Button type="button" variant="outline" onClick={reset}>{t("reset")}</Button>
          <Button type="button" variant="outline" onClick={() => setRevision(v => v + 1)}>{t("refresh")}</Button>
        </div>
      </form>
      {errorGate ? <FeatureGateNotice gate={errorGate} variant="page" />
        : error ? <ErrorBanner error={t("error")} />
          : !page ? <TableLoadingState rows={5} />
            : page.items.length === 0 ? <EmptyState icon={History} title={t("empty")} description={t("emptyDescription")} actionLabel={t("reset")} onAction={reset} compact />
              : <>
                <p className="text-sm text-muted-foreground" role="status">{t("total", { count: page.total })}</p>
                <Table>
                  <TableHeader><TableRow>{["time", "actor", "operation", "target", "outcome"].map(key => <TableHead key={key}>{t(key)}</TableHead>)}</TableRow></TableHeader>
                  <TableBody>{page.items.map(item => (
                    <TableRow key={item.id}>
                      <TableCell className="whitespace-nowrap align-top"><time dateTime={item.occurred_at}>{new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "medium" }).format(new Date(item.occurred_at))}</time></TableCell>
                      <TableCell className="max-w-64 break-all align-top">
                        <div>{item.actor_name || item.actor_user_id || t(item.surface === "system" ? "system" : "unknown")}</div>
                        {item.actor_name && <div className="text-xs text-muted-foreground">{item.actor_user_id}</div>}
                        {item.agent_id && <div className="text-xs">{t("agent", { id: item.agent_id })}</div>}
                        <div className="text-xs text-muted-foreground">{t(`surfaces.${item.surface}`)}</div>
                        {item.actor_user_id && item.surface !== "web" && item.surface !== "system" && <div className="text-xs text-muted-foreground">{t("accountAttribution")}</div>}
                      </TableCell>
                      <TableCell className="align-top">
                        {t(`operations.${item.operation}`)}
                        {item.count > 1 && <div className="text-xs">{t("count", { count: item.count })}</div>}
                        {item.changed_fields.length > 0 && <div className="max-w-52 break-words text-xs text-muted-foreground">{item.changed_fields.map(field => t(`fields.${field}`)).join(", ")}</div>}
                      </TableCell>
                      <TableCell className="max-w-64 break-all align-top">
                        <div>{item.target_id || item.subject_user_id || t("multiple")}</div>
                        {item.context_id && <div className="text-xs text-muted-foreground">{t("context", { id: item.context_id })}</div>}
                        {item.target_type === "memory" && item.context_id && item.target_id && item.count === 1 && <Link className="text-sm underline" href={`/workspace/contexts/${encodeURIComponent(item.context_id)}?tab=memories&memoryId=${encodeURIComponent(item.target_id)}`}>{t("details")}</Link>}
                        {item.target_type === "sleep" && item.target_id && <Link className="text-sm underline" href={`/workspace/sleep-reports/${encodeURIComponent(item.target_id)}`}>{t("details")}</Link>}
                      </TableCell>
                      <TableCell className="align-top">{t(`outcomes.${item.outcome}`)}</TableCell>
                    </TableRow>
                  ))}</TableBody>
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
