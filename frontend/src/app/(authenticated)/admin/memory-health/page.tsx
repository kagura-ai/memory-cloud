"use client";

/**
 * Admin Memory Health Page (#1211, #1225)
 *
 * Per-context memory-health report: a breakdown list (one graded entry per
 * owned context, plus an "unattributed" bucket for context-less signals)
 * with drill-down into the 3-section detail document (consolidation /
 * graph / retrieval with ok / warn / fail grading). Section notes arrive
 * as structured {code, params} records and are localized here — issue
 * references never render in the UI (they live in
 * docs/ops/memory-health-report.md). Admin-only, self-scoped.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Activity } from "lucide-react";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { CardLoadingState, TableLoadingState } from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { apiClient } from "@/lib/api";

type HealthStatus = "ok" | "warn" | "fail";

type HealthNote = {
  code: string;
  params: Record<string, unknown>;
};

type HealthSection = {
  status: HealthStatus;
  metrics: Record<string, unknown>;
  notes: HealthNote[];
};

type ContextEntry = {
  context_id: string | null;
  name: string | null;
  overall_status: HealthStatus;
  sections: Record<string, HealthStatus>;
};

type BreakdownResponse = {
  generated_at: string;
  overall_status: HealthStatus;
  contexts: ContextEntry[];
};

type DetailResponse = {
  generated_at: string;
  context_id: string | null;
  context_name: string | null;
  overall_status: HealthStatus;
  sections: Record<string, HealthSection>;
};

/** Query-param value selecting the context-less signal bucket. */
const UNATTRIBUTED_SCOPE = "unattributed";

/** Backend note codes with a message-catalog entry. Anything else renders
 * the generic fallback — never a crash, never a blank note. */
const KNOWN_NOTE_CODES = new Set([
  "latest_sleep_failed",
  "judge_failures",
  "failed_runs_recovered",
  "deferred_pairs",
  "merge_backlog_old",
  "edge_weight_violations",
  "cold_graph",
  "write_only_store",
]);

const STATUS_STYLES: Record<string, string> = {
  ok: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  warn: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
  fail: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
};

const STATUS_LABEL_KEYS: Record<string, string> = {
  ok: "status.ok",
  warn: "status.warn",
  fail: "status.fail",
};

const SECTION_LABEL_KEYS: Record<string, string> = {
  consolidation: "sections.consolidation",
  graph: "sections.graph",
  retrieval: "sections.retrieval",
};

function StatusBadge({ status }: { status: string }) {
  const t = useTranslations("admin.memoryHealth");
  const labelKey = STATUS_LABEL_KEYS[status];
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-semibold uppercase ${
        STATUS_STYLES[status] ?? ""
      }`}
      data-testid={`status-${status}`}
    >
      {labelKey ? t(labelKey) : status}
    </span>
  );
}

function NoteText({ note }: { note: HealthNote }) {
  const t = useTranslations("admin.memoryHealth");
  if (!KNOWN_NOTE_CODES.has(note.code)) {
    return <>{t("notes.unknown", { code: note.code })}</>;
  }
  return <>{t(`notes.${note.code}`, note.params as Record<string, string | number>)}</>;
}

function SectionCard({ name, section }: { name: string; section: HealthSection }) {
  const t = useTranslations("admin.memoryHealth");
  return (
    <div className="rounded-lg border p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="font-semibold">
          {SECTION_LABEL_KEYS[name] ? t(SECTION_LABEL_KEYS[name]) : name}
        </h2>
        <StatusBadge status={section.status} />
      </div>
      {section.notes.length > 0 && (
        <ul className="mb-3 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
          {section.notes.map((note, i) => (
            <li key={`${note.code}-${i}`}>
              <NoteText note={note} />
            </li>
          ))}
        </ul>
      )}
      <table className="w-full table-fixed text-xs">
        <tbody>
          {Object.entries(section.metrics).map(([key, value]) => (
            <tr key={key} className="border-t align-top">
              <td className="w-1/2 break-all py-1 pr-2 font-mono text-muted-foreground">{key}</td>
              {/* break-all: a long unbroken value (e.g. a JSON object) wraps
                  inside its own cell instead of overlaying the neighbor. */}
              <td className="w-1/2 break-all py-1 text-right font-mono">
                {value === null
                  ? t("emptyValue")
                  : typeof value === "object"
                    ? JSON.stringify(value)
                    : String(value)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AdminMemoryHealthPage() {
  const t = useTranslations("admin.memoryHealth");
  const [breakdown, setBreakdown] = useState<BreakdownResponse | null>(null);
  const [detail, setDetail] = useState<DetailResponse | null>(null);
  const [selectedScope, setSelectedScope] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const loadBreakdown = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiClient.get<BreakdownResponse>("/api/v1/admin/memory-health");
      setBreakdown(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (scope: string) => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiClient.get<DetailResponse>(
        `/api/v1/admin/memory-health?context_id=${encodeURIComponent(scope)}`,
      );
      setDetail(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadBreakdown();
  }, [loadBreakdown]);

  const openDetail = (entry: ContextEntry) => {
    const scope = entry.context_id ?? UNATTRIBUTED_SCOPE;
    setSelectedScope(scope);
    setDetail(null);
    void loadDetail(scope);
  };

  const backToList = () => {
    setSelectedScope(null);
    setDetail(null);
    void loadBreakdown();
  };

  const refresh = () => {
    if (selectedScope) {
      void loadDetail(selectedScope);
    } else {
      void loadBreakdown();
    }
  };

  const inDetailView = selectedScope !== null;

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">{t("description")}</p>
        </div>
        <div className="flex items-center gap-2">
          {inDetailView && (
            <button
              type="button"
              onClick={backToList}
              className="rounded border px-3 py-1 text-sm hover:bg-accent"
            >
              {t("back")}
            </button>
          )}
          <button
            type="button"
            onClick={refresh}
            className="rounded border px-3 py-1 text-sm hover:bg-accent"
          >
            {t("refresh")}
          </button>
        </div>
      </div>

      <ErrorBanner error={error} />

      {!inDetailView && (
        <>
          {loading && <TableLoadingState rows={4} />}
          {!loading && breakdown && (
            <>
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium">{t("overall")}</span>
                <StatusBadge status={breakdown.overall_status} />
                <span className="text-xs text-muted-foreground">{breakdown.generated_at}</span>
              </div>

              {breakdown.contexts.length === 0 ? (
                <EmptyState
                  icon={Activity}
                  title={t("emptyTitle")}
                  description={t("emptyDescription")}
                  compact
                />
              ) : (
                <div className="divide-y rounded-lg border">
                  {breakdown.contexts.map((entry) => (
                    <button
                      key={entry.context_id ?? UNATTRIBUTED_SCOPE}
                      type="button"
                      onClick={() => openDetail(entry)}
                      className="flex w-full flex-wrap items-center justify-between gap-2 px-4 py-3 text-left hover:bg-accent"
                      data-testid={`context-${entry.context_id ?? UNATTRIBUTED_SCOPE}`}
                    >
                      <div className="flex min-w-0 items-center gap-3">
                        <StatusBadge status={entry.overall_status} />
                        <span className="truncate font-medium">
                          {entry.name ?? t("unattributed")}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        {Object.entries(entry.sections).map(([name, status]) => (
                          <span key={name} className="flex items-center gap-1 text-xs">
                            <span className="text-muted-foreground">
                              {SECTION_LABEL_KEYS[name] ? t(SECTION_LABEL_KEYS[name]) : name}
                            </span>
                            <StatusBadge status={status} />
                          </span>
                        ))}
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </>
          )}
        </>
      )}

      {inDetailView && (
        <>
          {loading && <CardLoadingState count={3} />}
          {!loading && detail && (
            <>
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium">
                  {detail.context_name ?? t("unattributed")}
                </span>
                <StatusBadge status={detail.overall_status} />
                <span className="text-xs text-muted-foreground">{detail.generated_at}</span>
              </div>

              <div className="grid gap-4 md:grid-cols-3">
                {Object.entries(detail.sections).map(([name, section]) => (
                  <SectionCard key={name} name={name} section={section} />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
