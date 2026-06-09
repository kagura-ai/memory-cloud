/**
 * Resource Data tab — paginated ingest-event browser (Issue #316).
 *
 * Developer debug tool for "did my ingest write what I expected?". Lists
 * resource_events newest-first with cursor pagination, fixed filters
 * (op / doc_id / version / since), a schema-driven key-value view, and a
 * raw-JSON pane with copy. Payloads render lazily — only an expanded record
 * parses and shows its payload; over-cap payloads are flagged, not rendered.
 *
 * Replaces the #47 placeholder. Owner-only data (the endpoint enforces it).
 */

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Check, ChevronRight, Copy, Database } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { TableLoadingState } from "@/components/common/LoadingState";
import { useAuth } from "@/contexts/AuthContext";
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
import { useToast } from "@/hooks/use-toast";
import { formatDateTime } from "@/lib/utils/datetime";
import { formatBytes } from "@/lib/utils/format";
import {
  listResourceEvents,
  type ResourceEventRecord,
} from "@/lib/api/resources";
import type { FieldDefinition, ResourceSchema } from "@/lib/api/schemas";

const PAGE_SIZE = 20;
// Sentinel for the "all operations" Select option — Radix Select cannot use an
// empty-string value, so an explicit token maps back to "no op filter".
const OP_ALL = "all";

interface ResourceDataTabProps {
  resourceId: string;
  /** Latest schema (if registered) — drives the key-value type hints. */
  schema: ResourceSchema | null;
}

/** Applied (committed) filter set — distinct from the in-flight form state. */
interface AppliedFilters {
  op?: "upsert" | "delete";
  doc_id?: string;
  version?: number;
  since?: string;
}

export function ResourceDataTab({ resourceId, schema }: ResourceDataTabProps) {
  const t = useTranslations("resources.data");
  const locale = useLocale();
  const { user } = useAuth();
  const timezone = user?.timezone || "UTC";

  // Form state (uncommitted) vs applied filters (drive the fetch).
  const [opInput, setOpInput] = useState<string>(OP_ALL);
  const [docIdInput, setDocIdInput] = useState("");
  const [versionInput, setVersionInput] = useState("");
  const [sinceInput, setSinceInput] = useState("");
  const [applied, setApplied] = useState<AppliedFilters>({});

  const [events, setEvents] = useState<ResourceEventRecord[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Guards against out-of-order fetch responses (see fetchPage).
  const latestReqRef = useRef(0);

  // Field-name → definition map for the schema-driven key-value type hints.
  const fieldMap = useMemo(() => {
    const map = new Map<string, FieldDefinition>();
    for (const f of schema?.field_definitions ?? []) map.set(f.name, f);
    return map;
  }, [schema]);

  const fetchPage = useCallback(
    async (nextCursor: string | null, append: boolean) => {
      // Monotonic request id: if a newer fetch (e.g. an Apply fired while the
      // mount fetch is still in flight) starts before this one resolves, drop
      // this stale response so it can't overwrite the newer results.
      const reqId = ++latestReqRef.current;
      if (append) setLoadingMore(true);
      else setLoading(true);
      setError(null);
      try {
        const res = await listResourceEvents(resourceId, {
          ...applied,
          limit: PAGE_SIZE,
          cursor: nextCursor ?? undefined,
        });
        if (reqId !== latestReqRef.current) return; // superseded — ignore
        setEvents((prev) => (append ? [...prev, ...res.events] : res.events));
        setCursor(res.next_cursor);
      } catch (err) {
        if (reqId !== latestReqRef.current) return;
        setError(err instanceof Error ? err.message : t("fetchError"));
      } finally {
        if (reqId === latestReqRef.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [resourceId, applied, t],
  );

  // Re-fetch from page 1 whenever the applied filter set changes (incl. mount).
  useEffect(() => {
    fetchPage(null, false);
  }, [fetchPage]);

  const handleApply = () => {
    const next: AppliedFilters = {};
    if (opInput !== OP_ALL) next.op = opInput as "upsert" | "delete";
    if (docIdInput.trim()) next.doc_id = docIdInput.trim();
    if (versionInput.trim()) {
      const v = Number(versionInput.trim());
      if (Number.isInteger(v) && v >= 0) next.version = v;
    }
    if (sinceInput.trim()) {
      // <input type="datetime-local"> yields a tz-less wall-clock string
      // ("2026-06-07T12:00") in the browser's local zone. Convert to an
      // explicit UTC instant so the backend's naive-UTC comparison matches
      // the user's intent — sending the raw string would make a JST user's
      // "noon" filter at noon UTC (9h off).
      const d = new Date(sinceInput);
      if (!Number.isNaN(d.getTime())) next.since = d.toISOString();
    }
    setApplied(next);
  };

  const handleClear = () => {
    setOpInput(OP_ALL);
    setDocIdInput("");
    setVersionInput("");
    setSinceInput("");
    // Avoid a spurious refetch when nothing was applied — a fresh {} object
    // would still change the fetchPage dep identity and re-trigger the effect.
    setApplied((prev) => (Object.keys(prev).length === 0 ? prev : {}));
  };

  return (
    <div className="space-y-4">
      {/* Filter bar — fixed params only (no JSONB DSL). Applied on submit so
          typing does not spam the endpoint. */}
      <form
        className="flex flex-wrap items-end gap-3 rounded-lg border bg-card p-4"
        onSubmit={(e) => {
          e.preventDefault();
          handleApply();
        }}
      >
        <div className="space-y-1">
          <label className="text-xs text-muted-foreground">
            {t("filter.op")}
          </label>
          <Select value={opInput} onValueChange={setOpInput}>
            <SelectTrigger className="w-32" aria-label={t("filter.op")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={OP_ALL}>{t("filter.opAll")}</SelectItem>
              <SelectItem value="upsert">{t("op.upsert")}</SelectItem>
              <SelectItem value="delete">{t("op.delete")}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <label
            className="text-xs text-muted-foreground"
            htmlFor="filter-docid"
          >
            {t("filter.docId")}
          </label>
          <Input
            id="filter-docid"
            value={docIdInput}
            onChange={(e) => setDocIdInput(e.target.value)}
            placeholder={t("filter.docIdPlaceholder")}
            className="w-44"
          />
        </div>
        <div className="space-y-1">
          <label
            className="text-xs text-muted-foreground"
            htmlFor="filter-version"
          >
            {t("filter.version")}
          </label>
          <Input
            id="filter-version"
            type="number"
            min={0}
            value={versionInput}
            onChange={(e) => setVersionInput(e.target.value)}
            className="w-24"
          />
        </div>
        <div className="space-y-1">
          <label
            className="text-xs text-muted-foreground"
            htmlFor="filter-since"
          >
            {t("filter.since")}
          </label>
          <Input
            id="filter-since"
            type="datetime-local"
            value={sinceInput}
            onChange={(e) => setSinceInput(e.target.value)}
            className="w-52"
          />
        </div>
        <div className="flex gap-2">
          <Button type="submit" size="sm">
            {t("filter.apply")}
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={handleClear}>
            {t("filter.clear")}
          </Button>
        </div>
      </form>

      {error && <ErrorBanner error={error} />}

      {loading ? (
        <div className="rounded-lg border bg-card p-4">
          <TableLoadingState rows={5} />
        </div>
      ) : events.length === 0 ? (
        <EmptyState
          icon={Database}
          title={t("emptyTitle")}
          description={t("emptyDescription")}
          compact
        />
      ) : (
        <div className="space-y-2">
          {events.map((event) => (
            <EventRow
              key={event.id}
              event={event}
              fieldMap={fieldMap}
              timezone={timezone}
              locale={locale}
            />
          ))}
          {cursor && (
            <div className="flex justify-center pt-2">
              <Button
                variant="outline"
                size="sm"
                disabled={loadingMore}
                onClick={() => fetchPage(cursor, true)}
              >
                {loadingMore ? t("loadingMore") : t("loadMore")}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface EventRowProps {
  event: ResourceEventRecord;
  fieldMap: Map<string, FieldDefinition>;
  timezone: string;
  locale: string;
}

function EventRow({ event, fieldMap, timezone, locale }: EventRowProps) {
  const t = useTranslations("resources.data");
  // Lazy render: the payload view is only built when the row is open, so a
  // long list stays cheap and closed rows are metadata-only.
  const [open, setOpen] = useState(false);
  const { isCopied, copyToTarget } = useCopyFeedback();
  const { toast } = useToast();

  const copyKey = `event-${event.id}`;

  const handleCopyRaw = async () => {
    try {
      await copyToTarget(JSON.stringify(event.payload, null, 2), copyKey);
    } catch {
      toast({ description: t("payload.copyFailed"), variant: "destructive" });
    }
  };

  return (
    <details
      className="group rounded-lg border bg-card"
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-2.5 text-sm">
        <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
        <Badge variant={event.op === "delete" ? "destructive" : "secondary"}>
          {t(`op.${event.op}`)}
        </Badge>
        <span className="font-mono">{event.doc_id}</span>
        <span className="text-muted-foreground">
          {t("meta.version")}: {event.version ?? "—"}
        </span>
        <span className="ml-auto text-xs text-muted-foreground">
          {formatDateTime(event.created_at, timezone, locale)}
        </span>
      </summary>

      {open && (
        <div className="space-y-4 border-t px-4 py-3">
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs">
            <dt className="text-muted-foreground">
              {t("meta.idempotencyKey")}
            </dt>
            <dd className="font-mono">{event.idempotency_key ?? "—"}</dd>
            <dt className="text-muted-foreground">{t("meta.importance")}</dt>
            <dd className="font-mono">{event.importance}</dd>
            <dt className="text-muted-foreground">{t("meta.payloadSize")}</dt>
            <dd className="font-mono">{formatBytes(event.payload_bytes)}</dd>
          </dl>

          {event.payload_truncated ? (
            <p className="text-sm text-muted-foreground">
              {t("payload.tooLarge", {
                size: formatBytes(event.payload_bytes),
              })}
            </p>
          ) : event.payload === null ? (
            <p className="text-sm text-muted-foreground">
              {t("payload.empty")}
            </p>
          ) : (
            <>
              <KeyValueTable payload={event.payload} fieldMap={fieldMap} />
              <div>
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-xs font-medium text-muted-foreground">
                    {t("payload.raw")}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 gap-1.5 text-xs"
                    onClick={handleCopyRaw}
                  >
                    {isCopied(copyKey) ? (
                      <Check className="h-3.5 w-3.5" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                    {isCopied(copyKey)
                      ? t("payload.copied")
                      : t("payload.copy")}
                  </Button>
                </div>
                <pre className="max-h-80 overflow-auto rounded-md bg-muted p-3 text-xs">
                  <code>{JSON.stringify(event.payload, null, 2)}</code>
                </pre>
              </div>
            </>
          )}
        </div>
      )}
    </details>
  );
}

interface KeyValueTableProps {
  payload: Record<string, unknown>;
  fieldMap: Map<string, FieldDefinition>;
}

/** Top-level key/value table with schema type hints; nested values shown as
 *  compact JSON (the raw pane below carries the full structure). */
function KeyValueTable({ payload, fieldMap }: KeyValueTableProps) {
  const t = useTranslations("resources.data");
  const entries = Object.entries(payload);
  if (entries.length === 0) return null;

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("kv.field")}</TableHead>
            <TableHead>{t("kv.value")}</TableHead>
            <TableHead>{t("kv.type")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {entries.map(([key, value]) => {
            const def = fieldMap.get(key);
            const searchable = def?.index_hint === "searchable";
            const display =
              value === null || typeof value !== "object"
                ? String(value)
                : JSON.stringify(value);
            return (
              <TableRow key={key}>
                <TableCell className="font-mono text-xs">
                  <span className="flex items-center gap-1.5">
                    {key}
                    {searchable && (
                      <Badge variant="outline" className="text-[10px]">
                        {t("kv.searchable")}
                      </Badge>
                    )}
                  </span>
                </TableCell>
                <TableCell className="max-w-md truncate font-mono text-xs">
                  {display}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {def ? def.type : "—"}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
