"use client";

/**
 * Memory health report (#1211, #1225, #2764).
 *
 * The three-section detail (consolidation / graph / retrieval, each graded
 * ok / warn / fail) of one scope: a context the viewer created, or the
 * `unattributed` bucket of signals recorded outside those contexts. Used by
 * the Health tab of the context detail and by the unattributed dialog of the
 * context list. Section notes arrive as structured {code, params} records and
 * are localized here; an unknown code renders a generic fallback.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { RefreshCw } from "lucide-react";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { CardLoadingState } from "@/components/common/LoadingState";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/AuthContext";
import { formatDateTime } from "@/lib/utils/datetime";
import { cn } from "@/lib/utils";
import {
  getMemoryHealthDetail,
  type MemoryHealthDetail,
  type MemoryHealthNote,
  type MemoryHealthSection,
  type MemoryHealthStatus,
} from "@/lib/api/memory-health";

const STATUS_STYLES: Record<MemoryHealthStatus, string> = {
  ok: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  warn: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
  fail: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
};

const KNOWN_STATUSES = new Set<string>(["ok", "warn", "fail"]);
const KNOWN_SECTIONS = new Set<string>(["consolidation", "graph", "retrieval"]);

/** An ok / warn / fail pill; also the context list's per-row badge. */
export function MemoryHealthStatusBadge({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  const t = useTranslations("memoryHealth");
  const known = KNOWN_STATUSES.has(status);
  return (
    <span
      className={cn(
        "inline-block rounded px-2 py-0.5 text-xs font-semibold uppercase",
        known ? STATUS_STYLES[status as MemoryHealthStatus] : "",
        className,
      )}
      data-testid={`health-status-${status}`}
    >
      {known ? t(`status.${status}`) : status}
    </span>
  );
}

function NoteText({ note }: { note: MemoryHealthNote }) {
  const t = useTranslations("memoryHealth");
  // The message catalog is the single source of known note codes: an unknown
  // code renders the generic fallback (never crash, never blank).
  if (!t.has(`notes.${note.code}`)) {
    return <>{t("noteUnknown", { code: note.code })}</>;
  }
  return <>{t(`notes.${note.code}`, note.params as Record<string, string | number>)}</>;
}

function SectionCard({ name, section }: { name: string; section: MemoryHealthSection }) {
  const t = useTranslations("memoryHealth");
  const known = KNOWN_SECTIONS.has(name);
  return (
    <div className="rounded-lg border p-4" data-testid={`health-section-${name}`}>
      <div className="mb-1 flex items-center justify-between gap-2">
        <h3 className="font-semibold">{known ? t(`sections.${name}.title`) : name}</h3>
        <MemoryHealthStatusBadge status={section.status} />
      </div>
      {known && (
        <p className="mb-2 text-xs text-muted-foreground">
          {t(`sections.${name}.description`)}
        </p>
      )}
      {section.notes.length > 0 && (
        <ul className="mb-3 list-disc space-y-1 pl-4 text-sm">
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
              <td className="w-1/2 py-1 pr-2 font-mono text-muted-foreground">{key}</td>
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

/**
 * Fetches and renders the detail of `scope` (a context id or the
 * `unattributed` sentinel), with its own loading, error and refresh.
 */
export function MemoryHealthReport({ scope }: { scope: string }) {
  const t = useTranslations("memoryHealth");
  const locale = useLocale();
  const { user } = useAuth();
  const [detail, setDetail] = useState<MemoryHealthDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // A response lands only if no newer request superseded it.
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const data = await getMemoryHealthDetail(scope);
      if (seq === requestSeq.current) setDetail(data);
    } catch {
      if (seq === requestSeq.current) setError(t("loadError"));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [scope, t]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-4" data-testid="memory-health-report">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="max-w-3xl text-sm text-muted-foreground">{t("description")}</p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void load()}
          disabled={loading}
        >
          <RefreshCw className="mr-2 h-4 w-4" />
          {t("refresh")}
        </Button>
      </div>

      <ErrorBanner error={error} />

      {loading && !detail && <CardLoadingState count={3} />}

      {detail && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm font-medium">{t("overall")}</span>
            <MemoryHealthStatusBadge status={detail.overall_status} />
            <span className="text-xs text-muted-foreground">
              {t("generatedAt", {
                time: formatDateTime(detail.generated_at, user?.timezone, locale),
              })}
            </span>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            {Object.entries(detail.sections).map(([name, section]) => (
              <SectionCard key={name} name={name} section={section} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
