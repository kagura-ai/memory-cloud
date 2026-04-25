/**
 * MemoriesTabPanel
 *
 * Context-scoped memory list for the Memories tab on contexts/[id]
 * (Issue #433). Lists rows via `GET /api/v1/memory/list?context_id=...`,
 * hydrates each row to full `Memory` via `POST /reference` on view/edit/delete,
 * deletes via `POST /forget`, and edits via `PATCH /api/v1/memory/{id}`
 * (Issue #439).
 *
 * Create is still deferred — needs an MCP-shape form rewrite.
 */

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { FileText } from "lucide-react";
import { MemoriesTable } from "@/components/memories/MemoriesTable";
import { MemoryDetailDialog } from "@/components/memories/MemoryDetailDialog";
import { DeleteMemoryDialog } from "@/components/memories/DeleteMemoryDialog";
import { EditMemoryDialog } from "@/components/memories/EditMemoryDialog";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import { useToast } from "@/hooks/use-toast";
import { getMemories, referenceMemory } from "@/lib/api/memory";
import type {
  LinkedMemoryRef,
  Memory,
  MemoryListItem,
  MemoryReference,
} from "@/lib/types/memory";

interface MemoriesTabPanelProps {
  contextId: string;
}

const PAGE_SIZE = 50;
const MEMORY_ID_PARAM = "memoryId";

// ``MemoryListItem`` (id-only) becomes something table-renderable here for
// rows not yet hydrated. The table expects legacy composite-key columns
// (key / agent_name); use the summary as a display fallback so cells aren't
// blank. ``id`` is authoritative for row identity; other fields are cosmetic.
function listItemAsMemory(item: MemoryListItem): Memory {
  return {
    id: item.id,
    summary: item.summary,
    key: item.summary,
    value: "",
    scope: item.scope,
    type: item.type,
    agent_name: "",
    user_id: "",
    importance: item.importance,
    created_at: item.created_at,
    updated_at: item.updated_at,
  };
}

// Adapt the reference response to the legacy ``Memory`` shape the dialog
// expects. With Issue #434 the response now carries scope + updated_at, so
// this conversion is page-independent — a deep-link to a memory not on the
// current page (or in a different context) hydrates correctly without
// needing the list item.
function referenceAsMemory(ref: MemoryReference): Memory {
  return {
    id: ref.memory_id,
    summary: ref.summary,
    key: ref.summary,
    value: ref.content,
    scope: ref.scope,
    type: ref.type,
    agent_name: "",
    user_id: "",
    importance: ref.importance,
    tags: ref.tags,
    metadata: ref.details ?? undefined,
    created_at: ref.created_at,
    updated_at: ref.updated_at,
    source_uri: ref.source_uri,
    source_type: ref.source_type,
  };
}

type DialogTarget = "detail" | "delete" | "edit";

interface LinkedRefsState {
  outgoing: LinkedMemoryRef[];
  outgoingHasMore: boolean;
  incoming: LinkedMemoryRef[];
  incomingHasMore: boolean;
}

const EMPTY_LINKED_REFS: LinkedRefsState = {
  outgoing: [],
  outgoingHasMore: false,
  incoming: [],
  incomingHasMore: false,
};

export function MemoriesTabPanel({ contextId }: MemoriesTabPanelProps) {
  const t = useTranslations("contextDetail.memoriesPanel");
  const { toast } = useToast();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const memoryIdParam = searchParams.get(MEMORY_ID_PARAM);

  const [items, setItems] = useState<MemoryListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [hydrated, setHydrated] = useState<Memory | null>(null);
  // Issue #439: keep the raw `MemoryReference` alongside the adapted
  // `Memory`. The Edit dialog needs the canonical 6-field shape (summary /
  // content / type / importance / tags / details), which `referenceAsMemory`
  // discards (`details` lands in `metadata?`, `content` lands in `value`).
  // Storing both avoids a round-trip refetch when opening Edit and makes
  // patch responses cheap to swap in via a single `setHydratedRaw` call.
  const [hydratedRaw, setHydratedRaw] = useState<MemoryReference | null>(null);
  const [linkedRefs, setLinkedRefs] =
    useState<LinkedRefsState>(EMPTY_LINKED_REFS);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailNotFound, setDetailNotFound] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);

  // Race guard: when the user clicks row A then B before A's reference() has
  // resolved, the B-click sets `pendingHydrationRef.current = B.id`; when A
  // finally resolves we compare and discard the stale result. Without this
  // the last-resolving call wins, so A's data can land in a dialog opened
  // for B. Cleared on dialog close so a stale in-flight reference can't
  // re-open the dialog after the user dismissed it.
  const pendingHydrationRef = useRef<string | null>(null);

  // Bypass token for the deep-link useEffect: when handleView /
  // handleOpenLinkedMemory mutate the URL, they also call openWith()
  // directly with viaUrl=false (so a failure surfaces as a toast — the
  // user clicked, they expect a notification, not a silent EmptyState).
  // Without this token the URL change would re-fire the effect, issue a
  // duplicate referenceMemory call, AND swap the toast path for the
  // viaUrl=true notFound path. Setting the ref to the id about to be
  // pushed lets the effect skip exactly one cycle.
  const skipNextDeepLinkEffectRef = useRef<string | null>(null);

  // Replace the URL while preserving every other search param (e.g. ?tab=).
  // ``router.replace`` (not push) keeps history clean — opening/closing the
  // dialog does not stack new history entries the user has to back through.
  // Anchoring on ``pathname`` matches the canonical pattern in
  // ``hooks/useTabParam.ts`` and avoids leaving a bare trailing ``?`` when
  // every search param has been removed.
  const setMemoryIdParam = useCallback(
    (id: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (id) {
        params.set(MEMORY_ID_PARAM, id);
      } else {
        params.delete(MEMORY_ID_PARAM);
      }
      const qs = params.toString();
      router.replace(`${pathname}${qs ? `?${qs}` : ""}`);
    },
    [pathname, router, searchParams],
  );

  const fetchMemories = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const offset = (page - 1) * PAGE_SIZE;
      const response = await getMemories({
        context_id: contextId,
        limit: PAGE_SIZE,
        offset,
      });
      setItems(response.memories);
      setTotal(response.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [contextId, page, t]);

  useEffect(() => {
    fetchMemories();
  }, [fetchMemories]);

  // Open the detail dialog for ``id``. ``viaUrl=true`` paths come from the
  // deep-link effect and render an EmptyState in the dialog on failure
  // (rather than a toast) because the user navigated by URL — there is no
  // implicit row context to redirect them to.
  const openWith = useCallback(
    async (id: string, target: DialogTarget, viaUrl = false) => {
      pendingHydrationRef.current = id;
      try {
        const ref = await referenceMemory(id);
        if (pendingHydrationRef.current !== id) return;
        setHydrated(referenceAsMemory(ref));
        setHydratedRaw(ref);
        setLinkedRefs({
          outgoing: ref.outgoing_links ?? [],
          outgoingHasMore: !!ref.outgoing_has_more,
          incoming: ref.incoming_links ?? [],
          incomingHasMore: !!ref.incoming_has_more,
        });
        setDetailNotFound(false);
        if (target === "detail") setDetailOpen(true);
        else if (target === "edit") setEditOpen(true);
        else setDeleteOpen(true);
      } catch (err) {
        if (pendingHydrationRef.current !== id) return;
        if (viaUrl && target === "detail") {
          setHydrated(null);
          setHydratedRaw(null);
          setLinkedRefs(EMPTY_LINKED_REFS);
          setDetailNotFound(true);
          setDetailOpen(true);
          return;
        }
        toast({
          variant: "destructive",
          title: t("hydrateFailed"),
          description: err instanceof Error ? err.message : undefined,
        });
      }
    },
    [toast, t],
  );

  // Deep-link sync (Issue #434): when ?memoryId= appears in the URL, open
  // the dialog for that memory. We do not depend on `items.find(id)` —
  // hydration runs straight from the API so a URL referencing a memory on a
  // different page (or even a different context the user has access to)
  // works on first paint. The dependency on ``memoryIdParam`` only — not
  // ``openWith`` — avoids re-firing when ``items`` re-renders. Click-driven
  // URL mutations (handleView / handleOpenLinkedMemory) set
  // ``skipNextDeepLinkEffectRef`` so this effect does NOT issue a duplicate
  // referenceMemory and does NOT swap the click's toast path for the
  // URL-paste notFound path.
  useEffect(() => {
    if (!memoryIdParam) {
      setDetailOpen(false);
      setDetailNotFound(false);
      // URL-driven dismiss (back/forward, manual edit) bypasses
      // ``handleDetailOpenChange``, so the in-flight cleanup needs to fire
      // here too: reset hydrated state and clear ``pendingHydrationRef`` so
      // a late URL-driven referenceMemory response can't re-open the dialog
      // after the param has been removed.
      setHydrated(null);
      setHydratedRaw(null);
      setLinkedRefs(EMPTY_LINKED_REFS);
      pendingHydrationRef.current = null;
      return;
    }
    if (skipNextDeepLinkEffectRef.current === memoryIdParam) {
      skipNextDeepLinkEffectRef.current = null;
      return;
    }
    if (hydrated?.id === memoryIdParam && detailOpen) return;
    void openWith(memoryIdParam, "detail", true);
    // openWith is stable enough; we want the trigger to be the URL value
    // changing, not other panel state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoryIdParam]);

  const handleDetailOpenChange = useCallback(
    (next: boolean) => {
      setDetailOpen(next);
      if (!next) {
        // Drop the deep-link param so the URL matches dialog state. The
        // useEffect above will see memoryIdParam=null on the next render
        // and won't re-open.
        if (memoryIdParam) setMemoryIdParam(null);
        setDetailNotFound(false);
        // Cancel any in-flight referenceMemory: setting the ref to null
        // makes the openWith resolution path's identity check fail, so a
        // late-arriving response can't re-open the dialog the user just
        // closed.
        pendingHydrationRef.current = null;
      }
    },
    [memoryIdParam, setMemoryIdParam],
  );

  const handleView = useCallback(
    (memory: Memory) => {
      // Sync URL first so refresh / share works; openWith fires alongside.
      // The deep-link effect would otherwise see this URL change and issue
      // a duplicate request, swapping our toast-on-failure for silent
      // notFound — set the bypass token to skip one effect cycle.
      skipNextDeepLinkEffectRef.current = memory.id;
      setMemoryIdParam(memory.id);
      void openWith(memory.id, "detail");
    },
    [openWith, setMemoryIdParam],
  );

  const handleDelete = useCallback(
    (memory: Memory) => void openWith(memory.id, "delete"),
    [openWith],
  );

  const handleOpenLinkedMemory = useCallback(
    (id: string) => {
      // Backlink click in the References section — Issue #440 + #434
      // composition: the URL becomes the canonical pointer to the new
      // memory, the dialog rehydrates onto it. Same deep-link-effect
      // bypass as handleView — see the comment there.
      skipNextDeepLinkEffectRef.current = id;
      setMemoryIdParam(id);
      void openWith(id, "detail");
    },
    [openWith, setMemoryIdParam],
  );

  const handleDetailDelete = useCallback(() => {
    setDetailOpen(false);
    setDeleteOpen(true);
  }, []);

  // Issue #439: Edit affordance. The detail dialog already has a hydrated
  // memory in `hydratedRaw`, so opening Edit is a synchronous state flip
  // (no extra fetch). The detail dialog stays open underneath so the user
  // can return to it with a single Cancel.
  const handleDetailEdit = useCallback(() => {
    if (!hydratedRaw) return;
    setEditOpen(true);
  }, [hydratedRaw]);

  const handleEditOpenChange = useCallback((next: boolean) => {
    setEditOpen(next);
  }, []);

  const handleEditSuccess = useCallback(
    (updated: MemoryReference) => {
      // Swap in the patched ref: dialog closes, detail dialog re-renders
      // against the fresh data. `fetchMemories()` refreshes the table row
      // so summary/type/importance changes show without manual refresh.
      // Toast surfaces the success — frontend rule: button-driven mutation
      // success → toast (not banner).
      setHydratedRaw(updated);
      setHydrated(referenceAsMemory(updated));
      setEditOpen(false);
      toast({ title: t("editSuccess") });
      void fetchMemories();
    },
    [fetchMemories, toast, t],
  );

  const handleDeleteOpenChange = useCallback(
    (next: boolean) => {
      setDeleteOpen(next);
      // Cancel path (delete dialog dismissed without confirming): the
      // detail dialog was closed when delete opened, but the URL still
      // carries ``?memoryId=`` and the user expects to return to the
      // detail view. Re-open the detail dialog if hydrated state is
      // still around (it isn't on the success path — handleDeleteSuccess
      // clears ``hydrated`` before this fires).
      if (!next && hydrated) {
        setDetailOpen(true);
      }
    },
    [hydrated],
  );

  const handleDeleteSuccess = useCallback(() => {
    setDeleteOpen(false);
    setDetailOpen(false);
    setHydrated(null);
    setHydratedRaw(null);
    setLinkedRefs(EMPTY_LINKED_REFS);
    if (memoryIdParam) setMemoryIdParam(null);
    toast({ title: t("deleteSuccess") });

    // Avoid stranding the user on an empty page when the deleted row was the
    // last one on this page. Dropping `page` back triggers the fetch effect;
    // otherwise re-fetch the current page.
    if (items.length === 1 && page > 1) {
      setPage(page - 1);
    } else {
      void fetchMemories();
    }
  }, [
    fetchMemories,
    toast,
    t,
    items.length,
    page,
    memoryIdParam,
    setMemoryIdParam,
  ]);

  const displayRows = useMemo(() => items.map(listItemAsMemory), [items]);

  if (error) {
    return <ErrorBanner error={error} />;
  }

  if (!loading && items.length === 0 && !memoryIdParam) {
    return (
      <EmptyState
        icon={FileText}
        title={t("emptyTitle")}
        description={t("emptyDesc")}
      />
    );
  }

  return (
    <>
      <MemoriesTable
        memories={displayRows}
        loading={loading}
        onView={handleView}
        onDelete={handleDelete}
        page={page}
        pageSize={PAGE_SIZE}
        total={total}
        onPageChange={setPage}
      />
      <MemoryDetailDialog
        memory={hydrated}
        open={detailOpen}
        onOpenChange={handleDetailOpenChange}
        onEdit={hydratedRaw ? handleDetailEdit : undefined}
        onDelete={handleDetailDelete}
        notFound={detailNotFound}
        outgoingLinks={linkedRefs.outgoing}
        outgoingHasMore={linkedRefs.outgoingHasMore}
        incomingLinks={linkedRefs.incoming}
        incomingHasMore={linkedRefs.incomingHasMore}
        onOpenLinkedMemory={handleOpenLinkedMemory}
      />
      {hydrated && (
        <DeleteMemoryDialog
          memory={hydrated}
          open={deleteOpen}
          onOpenChange={handleDeleteOpenChange}
          onSuccess={handleDeleteSuccess}
        />
      )}
      {/* Both hydrated state slices are mutated together (see `openWith`,
          `handleEditSuccess`, `handleDeleteSuccess`); guarding on both
          keeps the EditMemoryDialog mount aligned with the DeleteMemoryDialog
          mount above and prevents a render with stale `hydratedRaw` if a
          future code path forgets to clear one of the slices. */}
      {hydrated && hydratedRaw && (
        <EditMemoryDialog
          memory={hydratedRaw}
          open={editOpen}
          onOpenChange={handleEditOpenChange}
          onSuccess={handleEditSuccess}
        />
      )}
    </>
  );
}
