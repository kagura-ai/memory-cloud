"use client";

/**
 * Admin Memory Health Page (#1211)
 *
 * Renders the consolidated memory-health report — the label-free runtime
 * self-diagnosis document (consolidation / graph / retrieval sections with
 * ok / warn / fail grading). Admin-only, self-scoped (Phase 1).
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { CardLoadingState } from "@/components/common/LoadingState";
import { apiClient } from "@/lib/api";

type HealthSection = {
  status: "ok" | "warn" | "fail";
  metrics: Record<string, unknown>;
  notes: string[];
};

type MemoryHealthResponse = {
  generated_at: string;
  overall_status: "ok" | "warn" | "fail";
  sections: Record<string, HealthSection>;
};

const STATUS_STYLES: Record<string, string> = {
  ok: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  warn: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
  fail: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-semibold uppercase ${
        STATUS_STYLES[status] ?? ""
      }`}
      data-testid={`status-${status}`}
    >
      {status}
    </span>
  );
}

export default function AdminMemoryHealthPage() {
  const t = useTranslations("admin.memoryHealth");
  const [report, setReport] = useState<MemoryHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiClient.get<MemoryHealthResponse>(
        "/api/v1/admin/memory-health",
      );
      setReport(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">{t("description")}</p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded border px-3 py-1 text-sm hover:bg-accent"
        >
          {t("refresh")}
        </button>
      </div>

      {loading && <CardLoadingState count={3} />}
      <ErrorBanner error={error} />

      {!loading && report && (
        <>
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium">{t("overall")}</span>
            <StatusBadge status={report.overall_status} />
            <span className="text-xs text-muted-foreground">
              {report.generated_at}
            </span>
          </div>

          <div className="grid gap-4 md:grid-cols-3">
            {Object.entries(report.sections).map(([name, section]) => (
              <div key={name} className="rounded-lg border p-4">
                <div className="mb-2 flex items-center justify-between">
                  <h2 className="font-semibold capitalize">{name}</h2>
                  <StatusBadge status={section.status} />
                </div>
                {section.notes.length > 0 && (
                  <ul className="mb-3 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
                    {section.notes.map((note) => (
                      <li key={note}>{note}</li>
                    ))}
                  </ul>
                )}
                <table className="w-full text-xs">
                  <tbody>
                    {Object.entries(section.metrics).map(([key, value]) => (
                      <tr key={key} className="border-t">
                        <td className="py-1 pr-2 font-mono text-muted-foreground">
                          {key}
                        </td>
                        <td className="py-1 text-right font-mono">
                          {value === null
                            ? "—"
                            : typeof value === "object"
                              ? JSON.stringify(value)
                              : String(value)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
