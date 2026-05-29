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

import { useCallback, useEffect, useRef, useState } from "react";
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
const TAG_PARAM = "tag";
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

  // #618: tracks the previous debounced query so we reset an active tag only
  // when the search actually *changes* (typing) — not on mount / shared links.
  const prevDebouncedQueryRef = useRef(debouncedQuery);

  const dialog = useMemoryDetailDialog({ memoryIdParam, setMemoryIdParam });

  // Issue #618: a single active tag filter, URL-driven (?tag=) so it's
  // shareable and survives refresh. Clicking a TagCloud tag toggles it; the
  // memory list re-fetches with an ANY-match tags filter (AND-ed with q).
  // Normalize at the source: a whitespace-only ?tag (e.g. ?tag=%20) is ignored
  // by getMemories, so collapse it to null here too — otherwise the chip /
  // hasFilter UI would show an "active" filter the API query doesn't apply.
  const activeTag = searchParams.get(TAG_PARAM)?.trim() || null;

  const setActiveTagFilter = useCallback(
    (tag: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (tag) params.set(TAG_PARAM, tag);
      else params.delete(TAG_PARAM);
      const next = params.toString();
      setPage(1);
      router.replace(`${pathname}${next ? `?${next}` : ""}`);
    },
    [searchParams, pathname, router],
  );

  const handleTagClick = useCallback(
    (tag: string) => setActiveTagFilter(tag === activeTag ? null : tag),
    [activeTag, setActiveTagFilter],
  );

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
    // #618: a *changed* search resets the active tag so a new query doesn't
    // silently AND with a stale tag (the cloud re-facets to the query). Mount
    // / shared-link loads (query unchanged) keep any ?tag intact.
    const queryChanged = debouncedQuery !== prevDebouncedQueryRef.current;
    prevDebouncedQueryRef.current = debouncedQuery;
    if (trimmed && queryChanged) {
      params.delete(TAG_PARAM);
    }
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
        tags: activeTag ? [activeTag] : undefined,
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
  }, [contextId, page, debouncedQuery, activeTag, t]);

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
          type="search"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          placeholder={t("search.placeholder")}
          aria-label={t("search.label")}
          className="pl-9"
        />
      </div>
      {activeTag && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {t("filter.activeLabel")}
          </span>
          <Badge variant="secondary" className="gap-1">
            {activeTag}
            <button
              type="button"
              onClick={() => setActiveTagFilter(null)}
              aria-label={t("filter.clearTag", { tag: activeTag })}
              className="ml-0.5 rounded hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
            >
              <X className="h-3 w-3" />
            </button>
          </Badge>
        </div>
      )}
      <TagCloud
        contextId={contextId}
        activeTag={activeTag}
        onTagClick={handleTagClick}
        q={debouncedQuery}
      />
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
  // Any active filter (text search OR tag) — distinguishes "no memories at all"
  // from "no matches for the current filter".
  const hasFilter = hasQuery || !!activeTag;

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
  // the tag in the message when a tag filter is active.
  if (!loading && items.length === 0 && hasFilter && !memoryIdParam) {
    return (
      <div className="space-y-4">
        {filterControls}
        <EmptyState
          icon={SearchX}
          title={t("noResultsTitle")}
          description={t("noResultsDesc", {
            query: activeTag && !hasQuery ? activeTag : debouncedQuery,
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
