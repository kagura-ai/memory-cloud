/**
 * RecallResultsList (#2132)
 *
 * The Memories tab's recall-mode result rows. Recall folds judge-verified
 * older versions of a fact under the newest one (#2123) and, with
 * include_superseded, returns shadowed memories annotated with
 * `superseded_by` (#1208). Each row shows both, so an older version never
 * disappears without a trace: the folded count plus one button per folded
 * memory id, and a "superseded" badge with a button to the newer memory.
 * Every button opens a memory through `onOpen(id)` — the caller routes that to
 * the memory detail dialog.
 */

import { Eye, SearchX } from "lucide-react";
import { useTranslations } from "next-intl";

import { SpinnerLoading } from "@/components/common/LoadingState";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { FoldedMemoryLinks } from "./FoldedMemoryLinks";
import type { RecallResultItem } from "@/lib/types/memory";

interface RecallResultsListProps {
  results: RecallResultItem[];
  loading: boolean;
  /** The query the results answer — echoed in the empty state. */
  query: string;
  onOpen: (memoryId: string) => void;
}

const LINK_CLASS =
  "rounded font-mono text-xs text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50";

export function RecallResultsList({
  results,
  loading,
  query,
  onOpen,
}: RecallResultsListProps) {
  const t = useTranslations("contextDetail.memoriesPanel.recall");

  if (loading && results.length === 0) {
    return (
      <div className="flex h-64 items-center justify-center">
        <SpinnerLoading size="md" message={t("loading")} />
      </div>
    );
  }

  if (!loading && results.length === 0) {
    return (
      <EmptyState
        icon={SearchX}
        title={t("noResultsTitle")}
        description={t("noResultsDesc", { query })}
      />
    );
  }

  return (
    <ul
      className="space-y-2"
      aria-busy={loading || undefined}
      aria-label={t("resultsLabel")}
    >
      {results.map((r) => {
        // Gate on the count, not the ids: the backend may cap the id list
        // later, and the count alone still leaves a trace.
        const folded = r.folded && r.folded.count > 0 ? r.folded : null;
        const newer = r.superseded_by;
        return (
          <li
            key={r.memory_id}
            className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 space-y-1">
                <p className="break-words font-medium">{r.summary}</p>
                <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                  {r.type && (
                    <code className="rounded bg-slate-100 px-2 py-0.5 dark:bg-slate-800">
                      {r.type}
                    </code>
                  )}
                  {typeof r.score === "number" && (
                    <span>
                      {t("score", { score: Math.round(r.score * 100) })}
                    </span>
                  )}
                  {newer && (
                    <>
                      <Badge variant="outline">{t("superseded")}</Badge>
                      <button
                        type="button"
                        className={LINK_CLASS}
                        onClick={() => onOpen(newer)}
                      >
                        {t("openNewer")}
                      </button>
                    </>
                  )}
                </div>
                {folded && (
                  <FoldedMemoryLinks
                    key={folded.memory_ids.join(",")}
                    ids={folded.memory_ids}
                    count={folded.count}
                    onOpen={onOpen}
                  />
                )}
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => onOpen(r.memory_id)}
                title={t("view")}
                aria-label={t("view")}
              >
                <Eye className="h-4 w-4" />
              </Button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
