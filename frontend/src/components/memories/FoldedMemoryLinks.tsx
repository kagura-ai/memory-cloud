"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { referenceMemory } from "@/lib/api/memory";
import { Button } from "@/components/ui/button";
import { InlineSpinner } from "@/components/common/LoadingState";

/** Read summaries only on expansion, without recording a reference adoption. */
export function FoldedMemoryLinks({ ids, count, onOpen }: {
  ids: string[];
  count: number;
  onOpen: (id: string) => void;
}) {
  const t = useTranslations("contextDetail.memoriesPanel.recall");
  const [expanded, setExpanded] = useState(false);
  const [summaries, setSummaries] = useState<Record<string, string>>({});
  const [loaded, setLoaded] = useState(0);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  // Unmounting (a re-run replacing the rows, a closed dialog) aborts the
  // summary reads still in flight instead of letting the batches run on.
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  async function load(start: number) {
    abortRef.current ??= new AbortController();
    const unmounted = abortRef.current.signal;
    setLoading(true);
    setFailed(false);
    const end = Math.min(start + 20, ids.length);
    setLoaded(end);
    let hasFailure = false;
    // Bound both each expansion and parallel requests. Errors keep detail links usable.
    for (let i = start; i < end; i += 4) {
      if (unmounted.aborted) return;
      const batch = ids.slice(i, Math.min(i + 4, end));
      const results = await Promise.allSettled(
        batch.map(async (id) => {
          // Each read ends on unmount or after 10 s, whichever comes first.
          const request = new AbortController();
          const abort = () => request.abort();
          unmounted.addEventListener("abort", abort, { once: true });
          const timer = setTimeout(abort, 10_000);
          try {
            const memory = await referenceMemory(id, { recordAccess: false, signal: request.signal });
            if (!unmounted.aborted) setSummaries((previous) => ({ ...previous, [id]: memory.summary }));
          } finally {
            clearTimeout(timer);
            unmounted.removeEventListener("abort", abort);
          }
        }),
      );
      results.forEach((result) => {
        if (result.status === "rejected") hasFailure = true;
      });
    }
    if (unmounted.aborted) return;
    setFailed(hasFailure);
    setLoading(false);
  }

  if (!ids.length) return <span>{t("folded", { count })}</span>;
  return (
    <div className="space-y-1 text-xs text-muted-foreground">
      <Button
        type="button" variant="link" size="sm" className="h-auto p-0 text-xs"
        aria-expanded={expanded}
        onClick={() => {
          setExpanded(!expanded);
          if (!expanded && !loaded && !loading) void load(0);
        }}
      >
        {t("folded", { count })}
      </Button>
      {expanded && (
        <div className="space-y-1">
          {loading && <InlineSpinner />}
          {ids.slice(0, loaded).map((id, index) => (
            <button
              key={id} type="button" title={id}
              className="block max-w-full rounded text-left text-xs text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
              onClick={() => onOpen(id)}
            >
              {summaries[id] || t("olderVersion", { number: index + 1 })}
            </button>
          ))}
          {failed && <p role="status">{t("summariesFailed")}</p>}
          {loaded < ids.length && !loading && (
            <Button type="button" variant="link" size="sm" onClick={() => void load(loaded)}>
              {t("moreVersions")}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
