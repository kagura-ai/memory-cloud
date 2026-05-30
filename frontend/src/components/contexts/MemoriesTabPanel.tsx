/**
 * MemoriesTabPanel
 *
 * Context-scoped memory list for the Memories tab on contexts/[id]
 * (Issue #433). Lists rows via `GET /api/v1/memory/list?context_id=...`,
 * hydrates each row to full `MemoryReference` via `POST /reference` on
 * view/edit/delete, deletes via `POST /forget`, and edits via
 * `PATCH /api/v1/memory/{id}` (Issue #439).
 *
 * Dialog state machine extracted to `useMemoryDetailDialog` (Issue #435)
 * so GraphTabPanel can reuse it.
 *
 * Issue #580: adds a debounced (300ms) search input that drives the
 * `q` query-string parameter end-to-end — URL-synced via `?q=` for
 * shareable links, page reset to 1 on every debounced change, two
 * distinct empty states (zero memories vs. zero matches).
 *
 * Create is still deferred — needs an MCP-shape form rewrite.
 */

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { FileText, Search, SearchX } from "lucide-react";
import { MemoriesTable } from "@/components/memories/MemoriesTable";
import { MemoryDetailDialog } from "@/components/memories/MemoryDetailDialog";
import { DeleteMemoryDialog } from "@/components/memories/DeleteMemoryDialog";
import { EditMemoryDialog } from "@/components/memories/EditMemoryDialog";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { useToast } from "@/hooks/use-toast";
import { useMemoryDetailDialog } from "@/hooks/useMemoryDetailDialog";
import { useMemoryIdParam } from "@/hooks/useMemoryIdParam";
import { getMemories } from "@/lib/api/memory";
import { TagCloud } from "@/components/contexts/TagCloud";
import { Badge } from "@/components/ui/badge";
import { X } from "lucide-react";
import type { MemoryListItem, MemoryReference } from "@/lib/types/memory";

interface MemoriesTabPanelProps {
  contextId: string;
}

const PAGE_SIZE = 50;
const SEARCH_PARAM = "q";
// #830: repeatable param for the multi-tag AND drill-down (?tags=a&tags=b).
// Supersedes #618's single `?tag=`.
const TAGS_PARAM = "tags";
const DEBOUNCE_MS = 300;

export function MemoriesTabPanel({ contextId }: MemoriesTabPanelProps) {
  const t = useTranslations("contextDetail.memoriesPanel");
  const { toast } = useToast();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [memoryIdParam, setMemoryIdParam] = useMemoryIdParam();

  // Seed both the controlled-input state and the debounced query from the
  // URL so the first paint reflects a shared link (`?q=foo`) without an
  // extra render cycle.
  const initialQuery = searchParams.get(SEARCH_PARAM) ?? "";
  const [searchInput, setSearchInput] = useState<string>(initialQuery);
  const [debouncedQuery, setDebouncedQuery] = useState<string>(initialQuery);

  const [items, setItems] = useState<MemoryListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Monotonic request id — guards against a slow earlier fetch resolving
  // after a faster later one and overwriting `items` with stale results.
  // Each call increments the ref; only the latest call's response is
  // committed to state.
  const requestIdRef = useRef(0);

  const dialog = useMemoryDetailDialog({ memoryIdParam, setMemoryIdParam });

  // #830: multi-tag AND drill-down set, URL-driven (?tags=a&tags=b) so it's
  // shareable and survives refresh. Normalized: trim, drop blanks, de-dupe
  // (order-preserving) — mirrors the backend's tag normalization so the chips
  // and the API query agree. AND-combined with `q` (both coexist; a changed
  // query no longer clears the tags — they drill down together).
  //
  // Memoize on the value-stable joined key, NOT the `searchParams` object:
  // `useSearchParams()` can return a fresh reference on every render, which
  // would give `selectedTags` a new array identity each render and make every
  // consumer (`fetchMemories`, TagCloud) re-fire on unrelated re-renders.
  const tagsParamKey = searchParams.getAll(TAGS_PARAM).join("\u0000");
  const selectedTags = useMemo(
    () =>
      Array.from(
        new Set(
          (tagsParamKey ? tagsParamKey.split("\u0000") : [])
            .map((s) => s.trim())
            .filter(Boolean),
        ),
      ),
    [tagsParamKey],
  );

  // Focus targets for the a11y focus-management contract (#830) + the live
  // region that announces facet changes to screen readers.
  const searchInputRef = useRef<HTMLInputElement>(null);
  const chipRowRef = useRef<HTMLDivElement>(null);
  const [announcement, setAnnouncement] = useState("");

  const setSelectedTags = useCallback(
    (next: string[]) => {
      const params = new URLSearchParams(searchParams.toString());
      params.delete(TAGS_PARAM);
      for (const tag of next) params.append(TAGS_PARAM, tag);
      const qs = params.toString();
      setPage(1);
      router.replace(`${pathname}${qs ? `?${qs}` : ""}`);
    },
    [searchParams, pathname, router],
  );

  // Clicking a cloud tag ADDS it to the drill-down (additive, not replace).
  // Selected tags are excluded from the cloud by the backend, so a click is
  // always an "add" — no toggle-off from the cloud (use the chips for that).
  const handleTagClick = useCallback(
    (tag: string) => {
      if (selectedTags.includes(tag)) return;
      setSelectedTags([...selectedTags, tag]);
      setAnnouncement(t("filter.announceAdded", { tag }));
    },
    [selectedTags, setSelectedTags, t],
  );

  const removeTag = useCallback(
    (tag: string) => {
      const idx = selectedTags.indexOf(tag);
      setSelectedTags(selectedTags.filter((x) => x !== tag));
      setAnnouncement(t("filter.announceRemoved", { tag }));
      // Move focus deterministically after the chip unmounts: the chip that
      // shifts into this index, else the now-last chip, else the search input.
      requestAnimationFrame(() => {
        const chips =
          chipRowRef.current?.querySelectorAll<HTMLButtonElement>(
            "[data-chip-remove]",
          );
        if (chips && chips.length > 0) {
          chips[Math.min(idx, chips.length - 1)]?.focus();
        } else {
          searchInputRef.current?.focus();
        }
      });
    },
    [selectedTags, setSelectedTags, t],
  );

  const clearAllTags = useCallback(() => {
    setSelectedTags([]);
    setAnnouncement(t("filter.announceCleared"));
    requestAnimationFrame(() => searchInputRef.current?.focus());
  }, [setSelectedTags, t]);

  // Debounce: schedule a single timer that promotes `searchInput` into
  // `debouncedQuery` AND resets page to 1 in one batched update. Doing
  // both inside the same setTimeout callback lets React 18 batch the two
  // state updates into a single render, so `fetchMemories` regenerates
  // exactly once per debounced change. Splitting the page reset into a
  // separate effect would cause a double fetch (first with the stale
  // page, then with page=1).
  useEffect(() => {
    if (searchInput === debouncedQuery) return;
    const timer = window.setTimeout(() => {
      setDebouncedQuery(searchInput);
      setPage(1);
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchInput, debouncedQuery]);

  // Sync the URL whenever the *debounced* query changes — per-keystroke
  // navigation would spam history and trigger refetches for every
  // intermediate prefix.
  useEffect(() => {
    const params = new URLSearchParams(searchParams.toString());
    const trimmed = debouncedQuery.trim();
    if (trimmed) {
      params.set(SEARCH_PARAM, trimmed);
    } else {
      params.delete(SEARCH_PARAM);
    }
    // #830: q and the selected tags drill down TOGETHER (AND), so a changed
    // query no longer clears the tags — unlike #618's single-tag reset.
    const next = params.toString();
    const current = searchParams.toString();
    if (next === current) return;
    router.replace(`${pathname}${next ? `?${next}` : ""}`);
    // We deliberately exclude `searchParams`/`router`/`pathname` from the
    // dependency array — those identities can flip on any URL change
    // (including ones we just made), and re-running would race with the
    // setMemoryIdParam writer. The debounced value is the only real input.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedQuery]);

  const fetchMemories = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const offset = (page - 1) * PAGE_SIZE;
      // Trim before sending — whitespace-only is equivalent to no filter
      // on the backend, but the client should not waste a request on it.
      const trimmed = debouncedQuery.trim();
      const response = await getMemories({
        context_id: contextId,
        limit: PAGE_SIZE,
        offset,
        q: trimmed || undefined,
        // #830: ALL-match so the list mirrors the cloud's AND drill-down.
        tags: selectedTags.length ? selectedTags : undefined,
        tagsMatch: selectedTags.length ? "all" : undefined,
      });
      if (requestId !== requestIdRef.current) return;
      setItems(response.memories);
      setTotal(response.total);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(err instanceof Error ? err.message : t("loadFailed"));
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [contextId, page, debouncedQuery, selectedTags, t]);

  useEffect(() => {
    fetchMemories();
  }, [fetchMemories]);

  const handleView = useCallback(
    (memory: MemoryListItem) => dialog.openDetail(memory.id),
    [dialog],
  );

  const handleDelete = useCallback(
    (memory: MemoryListItem) => dialog.openDelete(memory.id),
    [dialog],
  );

  const handleEditSuccess = useCallback(
    (updated: MemoryReference) => {
      dialog.applyEditSuccess(updated);
      toast({ title: t("editSuccess") });
      void fetchMemories();
    },
    [dialog, fetchMemories, toast, t],
  );

  const handleDeleteSuccess = useCallback(() => {
    dialog.applyDeleteSuccess();
    toast({ title: t("deleteSuccess") });

    // Avoid stranding the user on an empty page when the deleted row was the
    // last one on this page. Dropping `page` back triggers the fetch effect;
    // otherwise re-fetch the current page.
    if (items.length === 1 && page > 1) {
      setPage(page - 1);
    } else {
      void fetchMemories();
    }
  }, [dialog, fetchMemories, toast, t, items.length, page]);

  // Search input → active-tag chip → tag cloud, rendered above any state branch
  // (error / empty) so the user can always search, see/clear the active filter,
  // or discover tags. Search leads (primary, familiar control); the tag cloud —
  // which can be tall — sits below so it never pushes search off-screen.
  const filterControls = (
    <div className="space-y-3">
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400"
          aria-hidden="true"
        />
        <Input
          ref={searchInputRef}
          type="search"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          placeholder={t("search.placeholder")}
          aria-label={t("search.label")}
          className="pl-9"
        />
      </div>
      {/*
        #830: selected-tags chip row (generalized from #618's single chip).
        Each chip removes one tag from the AND drill-down; "clear all" resets
        the set. The "filtering N" count makes the additive model legible.
      */}
      {selectedTags.length > 0 && (
        <div ref={chipRowRef} className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {t("filter.activeLabel")}
          </span>
          {selectedTags.map((tag) => (
            <Badge key={tag} variant="secondary" className="gap-1">
              {tag}
              <button
                type="button"
                data-chip-remove
                onClick={() => removeTag(tag)}
                aria-label={t("filter.clearTag", { tag })}
                className="ml-0.5 rounded hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
          <button
            type="button"
            onClick={clearAllTags}
            className="rounded text-xs text-gray-500 underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 dark:text-gray-400"
          >
            {t("filter.clearAll")}
          </button>
          {/* Hide the count while a fetch is in flight so the chip row never
              shows a stale total (0 on first paint of a shared link, or the
              previous page's total during a refetch). */}
          {!loading && (
            <span className="text-xs text-gray-400 dark:text-gray-500">
              {t("filter.filteringCount", { count: total })}
            </span>
          )}
        </div>
      )}
      <TagCloud
        contextId={contextId}
        selectedTags={selectedTags}
        onTagClick={handleTagClick}
        q={debouncedQuery}
      />
      {/* a11y: announce facet changes — the cloud re-rendering is silent to AT. */}
      <div aria-live="polite" className="sr-only" role="status">
        {announcement}
      </div>
    </div>
  );

  if (error) {
    return (
      <div className="space-y-4">
        {filterControls}
        <ErrorBanner error={error} />
      </div>
    );
  }

  const hasQuery = debouncedQuery.trim().length > 0;
  // Any active filter (text search OR ≥1 selected tag) — distinguishes "no
  // memories at all" from "no matches for the current filter".
  const hasFilter = hasQuery || selectedTags.length > 0;

  // No memories at all in this context — keep the existing landing copy.
  if (!loading && items.length === 0 && !hasFilter && !memoryIdParam) {
    return (
      <div className="space-y-4">
        {filterControls}
        <EmptyState
          icon={FileText}
          title={t("emptyTitle")}
          description={t("emptyDesc")}
        />
      </div>
    );
  }

  // Filter produced no matches — distinct copy that echoes the filter so the
  // user can tell whether they mistyped vs. ran a too-narrow filter. Prefer
  // the selected tags in the message when a tag filter is active.
  if (!loading && items.length === 0 && hasFilter && !memoryIdParam) {
    return (
      <div className="space-y-4">
        {filterControls}
        <EmptyState
          icon={SearchX}
          title={t("noResultsTitle")}
          description={t("noResultsDesc", {
            query:
              selectedTags.length && !hasQuery
                ? selectedTags.join(", ")
                : debouncedQuery,
          })}
        />
      </div>
    );
  }

  return (
    <>
      <div className="space-y-4">
        {filterControls}
        <MemoriesTable
          memories={items}
          loading={loading}
          onView={handleView}
          onDelete={handleDelete}
          page={page}
          pageSize={PAGE_SIZE}
          total={total}
          onPageChange={setPage}
        />
      </div>
      <MemoryDetailDialog
        memory={dialog.hydrated}
        open={dialog.detailOpen}
        onOpenChange={dialog.handleDetailOpenChange}
        onEdit={dialog.hydrated ? dialog.handleDetailEdit : undefined}
        onDelete={dialog.handleDetailDelete}
        notFound={dialog.detailNotFound}
        outgoingLinks={dialog.linkedRefs.outgoing}
        outgoingHasMore={dialog.linkedRefs.outgoingHasMore}
        incomingLinks={dialog.linkedRefs.incoming}
        incomingHasMore={dialog.linkedRefs.incomingHasMore}
        onOpenLinkedMemory={dialog.openDetail}
      />
      {dialog.hydrated && (
        <DeleteMemoryDialog
          memory={dialog.hydrated}
          open={dialog.deleteOpen}
          onOpenChange={dialog.handleDeleteOpenChange}
          onSuccess={handleDeleteSuccess}
        />
      )}
      {dialog.hydrated && (
        <EditMemoryDialog
          memory={dialog.hydrated}
          open={dialog.editOpen}
          onOpenChange={dialog.handleEditOpenChange}
          onSuccess={handleEditSuccess}
          contextId={contextId}
        />
      )}
    </>
  );
}
