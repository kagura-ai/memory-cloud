/**
 * TagCloud (#618)
 *
 * Frequency-weighted tag cloud for the context detail page. Fetches
 * `GET /contexts/{id}/tags` and renders each tag as a real <button> sized by
 * count (sqrt scale, clamped to a few discrete steps). Clicking a tag toggles
 * the memory-list filter (the parent owns the active-tag state + URL sync).
 *
 * Design (gate1 / CDO): size encodes magnitude (NOT color — a11y + dark mode);
 * tags are buttons with aria-pressed + count in aria-label; a count/recent
 * toggle uses the Tabs primitive.
 */
"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Tag as TagIcon } from "lucide-react";
import {
  getContextTags,
  type ContextTagItem,
  type TagSortMode,
} from "@/lib/api/contexts";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { LoadingState } from "@/components/common/LoadingState";

const TAG_LIMIT = 50;
// Discrete sqrt-scaled buckets. Counts are long-tailed, so sqrt compresses the
// head; equal counts collapse to the base size (no divide-by-zero).
const SIZE_CLASSES = ["text-xs", "text-sm", "text-base", "text-lg"] as const;

function sizeClass(count: number, min: number, max: number): string {
  if (max <= min) return SIZE_CLASSES[1];
  const norm = Math.sqrt((count - min) / (max - min)); // 0..1
  const idx = Math.min(
    SIZE_CLASSES.length - 1,
    Math.floor(norm * SIZE_CLASSES.length),
  );
  return SIZE_CLASSES[idx];
}

export interface TagCloudProps {
  contextId: string;
  /** The tag currently driving the memory-list filter, or null. */
  activeTag: string | null;
  /** Toggle a tag as the active filter (parent clears when tag === activeTag). */
  onTagClick: (tag: string) => void;
  /** #618: facet the cloud to tags on memories matching this search query. */
  q?: string;
}

export function TagCloud({
  contextId,
  activeTag,
  onTagClick,
  q,
}: TagCloudProps) {
  const t = useTranslations("contextDetail.tagCloud");
  const [sort, setSort] = useState<TagSortMode>("count");
  const [tags, setTags] = useState<ContextTagItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const reqRef = useRef(0);

  useEffect(() => {
    const reqId = ++reqRef.current;
    setLoading(true);
    setError(null);
    getContextTags(contextId, { sort, limit: TAG_LIMIT, q })
      .then((res) => {
        if (reqId === reqRef.current) setTags(res.tags);
      })
      .catch((err) => {
        if (reqId === reqRef.current)
          setError(err instanceof Error ? err.message : t("loadFailed"));
      })
      .finally(() => {
        if (reqId === reqRef.current) setLoading(false);
      });
    // Invalidate the in-flight request on re-run / unmount so a late response
    // can't setState after the component is gone (the reqId guards above will
    // no longer match).
    return () => {
      reqRef.current++;
    };
  }, [contextId, sort, q, t]);

  const counts = tags.map((x) => x.count);
  const min = counts.length ? Math.min(...counts) : 0;
  const max = counts.length ? Math.max(...counts) : 0;

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
      <header className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-gray-100">
          <TagIcon className="h-4 w-4 text-gray-400" aria-hidden="true" />
          {t("title")}
        </div>
        <Tabs value={sort} onValueChange={(v) => setSort(v as TagSortMode)}>
          <TabsList>
            <TabsTrigger value="count">{t("sortCount")}</TabsTrigger>
            <TabsTrigger value="recent">{t("sortRecent")}</TabsTrigger>
          </TabsList>
        </Tabs>
      </header>

      {error ? (
        <ErrorBanner error={error} />
      ) : loading ? (
        // Shared skeleton primitive (frontend rules: never hand-roll skeletons).
        <LoadingState lines={2} />
      ) : tags.length === 0 ? (
        <EmptyState
          compact
          icon={TagIcon}
          title={t("emptyTitle")}
          description={t("emptyDesc")}
        />
      ) : (
        <div
          className="flex flex-wrap items-baseline gap-x-3 gap-y-2"
          role="group"
          aria-label={t("ariaLabel")}
        >
          {tags.map((item) => {
            const isActive = item.tag === activeTag;
            return (
              <button
                key={item.tag}
                type="button"
                onClick={() => onTagClick(item.tag)}
                aria-pressed={isActive}
                aria-label={t("tagWithCount", {
                  tag: item.tag,
                  count: item.count,
                })}
                className={[
                  sizeClass(item.count, min, max),
                  "rounded px-1.5 py-0.5 transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50",
                  isActive
                    ? "bg-primary/10 font-semibold text-primary ring-1 ring-primary/40"
                    : "text-gray-600 hover:text-primary dark:text-gray-300 dark:hover:text-primary",
                ].join(" ")}
              >
                {item.tag}
                <span className="ml-1 align-super text-[0.65em] text-gray-400">
                  {item.count}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}
