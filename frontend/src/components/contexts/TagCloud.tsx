/**
 * TagCloud (#618)
 *
 * Frequency-weighted tag cloud for the context detail page. Fetches
 * `GET /contexts/{id}/tags` and renders each tag as a real <button> sized by
 * count (stable absolute-count buckets — #830). Clicking a tag ADDS it to the
 * multi-tag AND drill-down (`selectedTags`); the parent owns the set + URL sync
 * and passes it back as `with_tags`, so selected tags are excluded from the
 * cloud body (the cloud always shows "what else can I add").
 *
 * Design (gate1 / CDO): size encodes magnitude (NOT color — a11y + dark mode)
 * on a stable basis so a tag's size doesn't jump on drill-down; a count/recent
 * toggle uses the Tabs primitive. aria-pressed was removed (#830) — selected
 * tags leave the cloud, so it would be permanently false.
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
// #830: absolute count buckets — a STABLE size basis. The pre-#830 scale
// normalized against the displayed set's min/max, so the same tag's font size
// jumped on every drill-down (the displayed set shrinks), eroding the
// "size = magnitude" signal. Fixed thresholds keep a tag's size constant
// regardless of how the cloud is faceted.
function sizeClass(count: number): string {
  if (count >= 21) return "text-lg";
  if (count >= 6) return "text-base";
  if (count >= 2) return "text-sm";
  return "text-xs";
}

export interface TagCloudProps {
  contextId: string;
  /**
   * #830: the multi-tag AND drill-down set. Sent as `with_tags` so the cloud
   * facets to co-occurring tags; the backend excludes these from the result,
   * so selected tags never appear in the cloud body.
   */
  selectedTags: string[];
  /** Additively add a clicked tag to the selected set (parent owns the set). */
  onTagClick: (tag: string) => void;
  /** #618: facet the cloud to tags on memories matching this search query. */
  q?: string;
}

export function TagCloud({
  contextId,
  selectedTags,
  onTagClick,
  q,
}: TagCloudProps) {
  const t = useTranslations("contextDetail.tagCloud");
  const [sort, setSort] = useState<TagSortMode>("count");
  const [tags, setTags] = useState<ContextTagItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const reqRef = useRef(0);

  // selectedTags is a fresh array each render; depend on a stable joined key so
  // the effect only re-fetches when the actual set changes. NUL is not a valid
  // tag character, so it is a collision-safe separator.
  const selectedKey = selectedTags.join("\u0000");

  useEffect(() => {
    const reqId = ++reqRef.current;
    setLoading(true);
    setError(null);
    getContextTags(contextId, {
      sort,
      limit: TAG_LIMIT,
      q,
      withTags: selectedTags,
    })
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
    // selectedTags is intentionally tracked via selectedKey (stable identity).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contextId, sort, q, selectedKey, t]);

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
        // #830: distinguish "no tags at all" from "drilled down to nothing".
        // When a drill-down (selectedTags or a non-blank q) is active, the
        // cloud emptying means there are no further co-occurring tags. Use the
        // trimmed q so a whitespace-only query (which the API ignores) does not
        // flip to the "refined" copy.
        <EmptyState
          compact
          icon={TagIcon}
          title={
            selectedTags.length > 0 || q?.trim()
              ? t("emptyRefinedTitle")
              : t("emptyTitle")
          }
          description={
            selectedTags.length > 0 || q?.trim()
              ? t("emptyRefinedDesc")
              : t("emptyDesc")
          }
        />
      ) : (
        <div
          className="flex flex-wrap items-baseline gap-x-3 gap-y-2"
          role="group"
          aria-label={t("ariaLabel")}
        >
          {/*
            #830: selected tags are excluded from the cloud by the backend
            (with_tags self-exclusion), so every rendered tag is an
            "add to the drill-down" action. aria-pressed was removed — it
            would be permanently false here and misleads screen readers into
            announcing a toggle state that never toggles.
          */}
          {tags.map((item) => (
            <button
              key={item.tag}
              type="button"
              onClick={() => onTagClick(item.tag)}
              aria-label={t("tagAddWithCount", {
                tag: item.tag,
                count: item.count,
              })}
              className={[
                sizeClass(item.count),
                "rounded px-1.5 py-0.5 transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50",
                "text-gray-600 hover:text-primary dark:text-gray-300 dark:hover:text-primary",
              ].join(" ")}
            >
              {item.tag}
              <span className="ml-1 align-super text-[0.65em] text-gray-400">
                {item.count}
              </span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
