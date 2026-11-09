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
 * Issue #2132: a "Recall" search mode next to the text match. It runs
 * `POST /api/v1/memory/recall` on submit (never per keystroke — each recall
 * is an embedding call), shows the older versions recall folded under a row
 * and offers the `include_superseded` switch (every version as its own row,
 * shadowed memories with `superseded_by`). Mode and switch are local state:
 * a shared link never runs a recall by itself.
 *
 * Create is still deferred — needs an MCP-shape form rewrite.
 */

"use client";

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { ContextualHelp } from "@/components/common/ContextualHelp";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { FileText, Search, SearchX } from "lucide-react";
import { MemoriesTable } from "@/components/memories/MemoriesTable";
import { MemoryDetailDialog } from "@/components/memories/MemoryDetailDialog";
import { DeleteMemoryDialog } from "@/components/memories/DeleteMemoryDialog";
import { BulkDeleteMemoriesDialog } from "@/components/memories/BulkDeleteMemoriesDialog";
import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { requestCapacityRefresh } from "@/lib/capacityRefresh";
import { EditMemoryDialog } from "@/components/memories/EditMemoryDialog";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { InlineSpinner } from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { useToast } from "@/hooks/use-toast";
import { useMemoryDetailDialog } from "@/hooks/useMemoryDetailDialog";
import { useMemoryIdParam } from "@/hooks/useMemoryIdParam";
import { getMemories, recallMemories } from "@/lib/api/memory";
import { TagCloud } from "@/components/contexts/TagCloud";
import { RecallResultsList } from "@/components/memories/RecallResultsList";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { X } from "lucide-react";
import type {
  MemoryListItem,
  MemoryReference,
  RecallResultItem,
} from "@/lib/types/memory";

interface MemoriesTabPanelProps {
  contextId: string;
  recallOnly?: boolean;
}

const PAGE_SIZE = 50;
const SEARCH_PARAM = "q";
// #830: repeatable param for the multi-tag AND drill-down (?tags=a&tags=b).
// Supersedes #618's single `?tag=`.
const TAGS_PARAM = "tags";
const DEBOUNCE_MS = 300;
// The API returns top-k, not offset pages. Increasing k replaces the ranking.
const RECALL_K = 20;
const MAX_RECALL_K = 100;

/** Everything about a recall request except k: "show more" keeps it. */
function recallScope(request: { query: string; include_superseded: boolean; filters: object }) {
  return JSON.stringify([request.query, request.include_superseded, request.filters]);
}

type SearchMode = "text" | "recall";

export function MemoriesTabPanel({ contextId, recallOnly = false }: MemoriesTabPanelProps) {
  const t = useTranslations("contextDetail.memoriesPanel");
  const tRecall = useTranslations("contextDetail.memoriesPanel.recall");
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
  const [settledListRequest, setSettledListRequest] = useState<object | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  // Monotonic request id — guards against a slow earlier fetch resolving
  // after a faster later one and overwriting `items` with stale results.
  // Each call increments the ref; only the latest call's response is
  // committed to state.
  const requestIdRef = useRef(0);

  // #2132: recall mode. `recallRequest` is the submitted query (a fresh
  // object per submit or refresh, so re-submitting the same text re-runs);
  // null until the first submit. The switch and the tag filter re-run the
  // submitted query through the effect below. `recallOutcome` records which
  // effective request its rows answer, so loading is derived, not stored.
  const [searchMode, setSearchMode] = useState<SearchMode>(recallOnly ? "recall" : "text");
  const [includeSuperseded, setIncludeSuperseded] = useState(false);
  const [recallRequest, setRecallRequest] = useState<{ query: string; k: number; scopeKey: string } | null>(
    null,
  );
  const [recallOutcome, setRecallOutcome] = useState<{
    request: object;
    scope: string;
    k: number;
    results: RecallResultItem[];
    error: string | null;
  } | null>(null);

  // Re-run the submitted recall after an edit / delete / accepted supersede
  // (no-op before one).
  const refreshRecall = useCallback(() => {
    setRecallRequest((r) => (r ? { ...r } : r));
  }, []);

  const dialog = useMemoryDetailDialog({
    memoryIdParam,
    setMemoryIdParam,
    // #2515: the recall rows cache the last query, so a row that just became
    // superseded would stay until the next search. List mode is unchanged.
    onSupersedeAccepted: refreshRecall,
  });

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
    // #2132: recall mode runs on submit; the text match stays where it was.
    if (searchMode !== "text") return;
    if (searchInput === debouncedQuery) return;
    const timer = window.setTimeout(() => {
      setDebouncedQuery(searchInput);
      setPage(1);
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchInput, debouncedQuery, searchMode]);

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

  const listParams = useMemo(() => ({
    enabled: searchMode === "text",
    query: {
      context_id: contextId,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      q: debouncedQuery.trim() || undefined,
      tags: selectedTags.length ? selectedTags : undefined,
      tagsMatch: selectedTags.length ? "all" as const : undefined,
    },
  }), [contextId, page, debouncedQuery, selectedTags, searchMode]);

  const fetchMemories = useCallback(() => {
    // #2132: recall mode never renders the list; switching back to text
    // mode changes this callback's identity, so the effect below refetches.
    if (!listParams.enabled) return;
    const requestId = ++requestIdRef.current;
    return getMemories(listParams.query).then((response) => {
      if (requestId !== requestIdRef.current) return;
      setItems(response.memories);
      setTotal(response.total);
      setListError(null);
      setSettledListRequest(listParams);
    }, (err: unknown) => {
      if (requestId !== requestIdRef.current) return;
      setListError(err instanceof Error ? err.message : t("loadFailed"));
      setSettledListRequest(listParams);
    });
  }, [listParams, t]);
  const loading = settledListRequest !== listParams;
  const error = loading ? null : listError;

  useEffect(() => {
    fetchMemories();
  }, [fetchMemories]);

  // The submitted recall's full request. Its identity changes only when the
  // query is (re)submitted or refreshed, or the switch, context or tags
  // change — never on a mode switch, so flipping Text → Recall shows the
  // last rows without spending another recall.
  const scopeKey = JSON.stringify([contextId, selectedTags, includeSuperseded]);
  if (recallRequest && recallRequest.scopeKey !== scopeKey) {
    // Reset when filters change, including when returning to an earlier filter set.
    setRecallRequest({ ...recallRequest, k: RECALL_K, scopeKey });
  }
  const recallK = recallRequest?.scopeKey === scopeKey ? recallRequest.k : RECALL_K;
  const recallParams = useMemo(
    () =>
      recallRequest
        ? {
            query: recallRequest.query,
            k: recallK,
            include_superseded: includeSuperseded,
            filters: {
              context_id: contextId,
              // ALL-match, as the list's tag drill-down.
              ...(selectedTags.length
                ? { tags: selectedTags, tags_match: "all" as const }
                : {}),
            },
          }
        : null,
    [recallRequest, recallK, includeSuperseded, contextId, selectedTags],
  );
  const outcomeRequest = recallOutcome?.request;

  // A recall's response is kept only if its request is still the current
  // one when it lands, and the component is still mounted. The checks read
  // refs instead of an effect cleanup, so a mode switch never drops an
  // in-flight recall: switching Text -> Recall while one is pending waits
  // for it instead of sending it again.
  const latestRecallParamsRef = useRef(recallParams);
  const inFlightRecallRef = useRef<object | null>(null);
  const searchModeRef = useRef(searchMode);
  const mountedRef = useRef(true);
  useEffect(() => {
    latestRecallParamsRef.current = recallParams;
    searchModeRef.current = searchMode;
  }, [recallParams, searchMode]);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Runs only in recall mode and only when the rows shown do not answer
  // the current request: a tag change or a refresh in text mode is picked
  // up on return, and nothing runs before the first submit.
  useEffect(() => {
    if (!recallParams || searchMode !== "recall") return;
    if (outcomeRequest === recallParams) return;
    if (inFlightRecallRef.current === recallParams) return;
    const request = recallParams;
    inFlightRecallRef.current = request;
    // An obsolete response (a later request superseded it) or one landing
    // after unmount is dropped.
    const current = () => {
      if (inFlightRecallRef.current === request) {
        inFlightRecallRef.current = null;
      }
      return mountedRef.current && latestRecallParamsRef.current === request;
    };
    recallMemories(request).then(
      (response) => {
        if (!current()) return;
        setRecallOutcome({
          request,
          scope: recallScope(request),
          k: request.k,
          results: response.results,
          error: null,
        });
        if (searchModeRef.current === "recall") {
          setAnnouncement(
            tRecall("resultsCount", { count: response.results.length }),
          );
        }
      },
      (err: unknown) => {
        if (!current()) return;
        setRecallOutcome({
          request,
          scope: recallScope(request),
          k: request.k,
          results: [],
          error: err instanceof Error ? err.message : tRecall("loadFailed"),
        });
      },
    );
  }, [recallParams, searchMode, outcomeRequest, tRecall]);

  const recallLoading =
    recallParams !== null && outcomeRequest !== recallParams;
  // "Show more": the same query and scope re-run with a larger k. The shown
  // page stays (busy) and so does its button, so focus and scroll stay put.
  const recallGrowing =
    recallLoading &&
    recallParams !== null &&
    recallOutcome !== null &&
    recallOutcome.error === null &&
    recallOutcome.scope === recallScope(recallParams) &&
    recallParams.k > recallOutcome.k;
  const recallError = recallLoading ? null : (recallOutcome?.error ?? null);
  // Rows of any other earlier request are never shown under a new one: while
  // such a re-run is in flight the list shows its loading state instead.
  const recallResults = recallLoading
    ? recallGrowing && recallOutcome
      ? recallOutcome.results
      : []
    : (recallOutcome?.results ?? []);
  // A page shorter than k: the ranking ran out (the fold and shadowing run
  // on the pool before the top-k cut). With a k-sized pool (neural off) a
  // fold can still shorten it, so the hint only says how many are shown.
  const canLoadMore =
    recallK < MAX_RECALL_K && (recallGrowing || recallResults.length >= recallK);
  const loadMoreClickedRef = useRef(false);
  const limitHintRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    // The button the user just pressed is gone (the ranking ran out or hit
    // the maximum): keep keyboard focus next to it instead of on <body>.
    if (!canLoadMore && loadMoreClickedRef.current) limitHintRef.current?.focus();
    loadMoreClickedRef.current = false;
    // Runs when a page lands (a new outcome) or the button's availability changes.
  }, [canLoadMore, recallOutcome]);
  const includeSupersededId = useId();
  const includeSupersededHelpId = useId();

  const submitRecall = useCallback(() => {
    const trimmed = searchInput.trim();
    if (!trimmed) return;
    setRecallRequest({ query: trimmed, k: RECALL_K, scopeKey });
  }, [searchInput, scopeKey]);

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
      refreshRecall();
    },
    [dialog, fetchMemories, refreshRecall, toast, t],
  );

  // #1941: delete by filter (date range / type / tags).
  const tBulk = useTranslations("contextDetail.bulkDelete");
  const [bulkOpen, setBulkOpen] = useState(false);
  const handleBulkDeleted = useCallback(
    (deleted: number) => {
      setBulkOpen(false);
      toast({ title: tBulk("deleted", { count: deleted }) });
      requestCapacityRefresh();
      refreshRecall();
      if (page > 1) {
        setPage(1);
      } else {
        void fetchMemories();
      }
    },
    [fetchMemories, refreshRecall, toast, tBulk, page],
  );

  // A later batch failed: the dialog keeps the error; refresh quietly.
  const handleBulkPartial = useCallback(() => {
    requestCapacityRefresh();
    refreshRecall();
    void fetchMemories();
  }, [fetchMemories, refreshRecall]);

  const handleDeleteSuccess = useCallback(() => {
    dialog.applyDeleteSuccess();
    toast({ title: t("deleteSuccess") });
    requestCapacityRefresh();
    refreshRecall();

    // Avoid stranding the user on an empty page when the deleted row was the
    // last one on this page. Dropping `page` back triggers the fetch effect;
    // otherwise re-fetch the current page.
    if (items.length === 1 && page > 1) {
      setPage(page - 1);
    } else {
      void fetchMemories();
    }
  }, [dialog, fetchMemories, refreshRecall, toast, t, items.length, page]);

  // Search input → active-tag chip → tag cloud, rendered above any state branch
  // (error / empty) so the user can always search, see/clear the active filter,
  // or discover tags. Search leads (primary, familiar control); the tag cloud —
  // which can be tall — sits below so it never pushes search off-screen.
  const filterControls = (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* #2132: text match (list) vs recall (semantic). */}
        {!recallOnly && <ToggleGroup
          type="single"
          value={searchMode}
          onValueChange={(mode) => { if (mode === "text" || mode === "recall") setSearchMode(mode); }}
          aria-label={tRecall("modeLabel")}
        >
          {(["text", "recall"] as const).map((mode) => (
            <ToggleGroupItem
              key={mode}
              value={mode}
            >
              {mode === "text" ? tRecall("modeText") : tRecall("modeRecall")}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>}
        <ContextualHelp topic="search" />
        {!recallOnly && <Button variant="outline" size="sm" onClick={() => setBulkOpen(true)}>
          {tBulk("open")}
        </Button>}
      </div>
      <form
        role="search"
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (searchMode === "recall") submitRecall();
        }}
      >
        <div className="relative flex-1">
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
        {searchMode === "recall" && (
          <Button type="submit" disabled={!searchInput.trim() || recallLoading}>
            {tRecall("submit")}
          </Button>
        )}
      </form>
      {searchMode === "recall" && (
        <div className="space-y-2">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {tRecall("hint")}
          </p>
          <div className="flex items-start gap-3">
            <Switch
              id={includeSupersededId}
              checked={includeSuperseded}
              onCheckedChange={setIncludeSuperseded}
              aria-describedby={includeSupersededHelpId}
            />
            <div className="space-y-0.5">
              <Label htmlFor={includeSupersededId}>
                {tRecall("includeSuperseded")}
              </Label>
              <p
                id={includeSupersededHelpId}
                className="text-xs text-gray-500 dark:text-gray-400"
              >
                {tRecall("includeSupersededHelp")}
              </p>
            </div>
          </div>
        </div>
      )}
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
          {!loading && searchMode === "text" && (
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
        // #2132: the text query narrows the cloud only in text mode; recall
        // rows are not a substring match, so the cloud covers the context.
        q={searchMode === "text" ? debouncedQuery : ""}
      />
      {/* a11y: announce facet changes — the cloud re-rendering is silent to AT. */}
      <div aria-live="polite" className="sr-only" role="status">
        {announcement}
      </div>
    </div>
  );

  const dialogs = (
    <>
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
        supersedeCandidate={dialog.supersedeCandidate}
        onAcceptSupersede={dialog.acceptSupersede}
        supersedeAccepting={dialog.supersedeAccepting}
        supersedeError={dialog.supersedeError}
      />
      <BulkDeleteMemoriesDialog
        contextId={contextId}
        open={bulkOpen}
        onOpenChange={setBulkOpen}
        onDeleted={handleBulkDeleted}
        onPartial={handleBulkPartial}
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

  // #2132: recall mode owns its own result / empty / error states; the
  // list's branches below stay the text match's.
  if (searchMode === "recall") {
    return (
      <>
        <div className="space-y-4">
          {filterControls}
          {recallError ? (
            <ErrorBanner error={recallError} />
          ) : recallRequest ? (
            <RecallResultsList
              results={recallResults}
              loading={recallLoading}
              query={recallRequest.query}
              onOpen={dialog.openDetail}
            />
          ) : null}
          {recallRequest && (!recallLoading || recallGrowing) && !recallError && recallResults.length > 0 && (
            <div className="space-y-2">
              <p ref={limitHintRef} tabIndex={-1} className="text-xs text-muted-foreground focus:outline-none">
                {canLoadMore
                  ? // While a larger page loads, describe the page on screen.
                    tRecall("limitHint", { limit: recallGrowing && recallOutcome ? recallOutcome.k : recallK })
                  : recallK >= MAX_RECALL_K && recallResults.length >= MAX_RECALL_K
                    ? tRecall("limitHintMax", { limit: MAX_RECALL_K })
                    : tRecall("limitHintAll", { count: recallResults.length })}
              </p>
              {canLoadMore && (
                <Button
                  type="button"
                  variant="outline"
                  // aria-disabled, not disabled: a disabled button drops focus.
                  aria-disabled={recallGrowing || undefined}
                  onClick={() => {
                    if (recallGrowing) return;
                    loadMoreClickedRef.current = true;
                    setRecallRequest({ ...recallRequest, k: Math.min(recallK + RECALL_K, MAX_RECALL_K), scopeKey });
                  }}
                >
                  {recallGrowing && <InlineSpinner className="mr-2" />}
                  {tRecall("loadMore")}
                </Button>
              )}
            </div>
          )}
        </div>
        {dialogs}
      </>
    );
  }

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
      {dialogs}
    </>
  );
}
