/**
 * MemoriesTabPanel
 *
 * Context-scoped memory list for the Memories tab on contexts/[id]
 * (Issue #433). Lists rows via `GET /api/v1/memory/list?context_id=...`,
 * hydrates each row to full `Memory` via `POST /reference` on view/delete,
 * and deletes via `POST /forget`.
 *
 * Edit and Create are deferred — no UUID-addressed PUT endpoint exists yet
 * (backend follow-up) and Create needs an MCP-shape form rewrite.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { FileText } from "lucide-react";
import { MemoriesTable } from "@/components/memories/MemoriesTable";
import { MemoryDetailDialog } from "@/components/memories/MemoryDetailDialog";
import { DeleteMemoryDialog } from "@/components/memories/DeleteMemoryDialog";
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

// ``POST /api/v1/memory/reference`` does NOT return {key, value, agent_name,
// user_id, updated_at, access_count, metadata}. Merge the response with the
// list item (which carries scope + updated_at) and fill the truly-absent
// legacy fields with empty strings so the detail dialog renders without
// crashing. This is the boundary where structural unsoundness is resolved.
function referenceAsMemory(ref: MemoryReference, item: MemoryListItem): Memory {
  return {
    id: ref.memory_id,
    summary: ref.summary,
    key: ref.summary,
    value: ref.content,
    scope: item.scope,
    type: ref.type,
    agent_name: "",
    user_id: "",
    importance: ref.importance,
    tags: ref.tags,
    metadata: ref.details ?? undefined,
    created_at: ref.created_at,
    updated_at: item.updated_at,
    source_uri: ref.source_uri,
    source_type: ref.source_type,
  };
}

type DialogTarget = "detail" | "delete";

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

  const [items, setItems] = useState<MemoryListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [hydrated, setHydrated] = useState<Memory | null>(null);
  const [linkedRefs, setLinkedRefs] =
    useState<LinkedRefsState>(EMPTY_LINKED_REFS);
  const [detailOpen, setDetailOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  // Race guard: when the user clicks row A then B before A's reference() has
  // resolved, the B-click sets `pendingHydrationRef.current = B.id`; when A
  // finally resolves we compare and discard the stale result. Without this
  // the last-resolving call wins, so A's data can land in a dialog opened
  // for B.
  const pendingHydrationRef = useRef<string | null>(null);

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

  const openWith = useCallback(
    async (id: string, target: DialogTarget) => {
      const item = items.find((m) => m.id === id);
      if (!item) return;

      pendingHydrationRef.current = id;
      try {
        const ref = await referenceMemory(id);
        if (pendingHydrationRef.current !== id) return;
        setHydrated(referenceAsMemory(ref, item));
        setLinkedRefs({
          outgoing: ref.outgoing_links ?? [],
          outgoingHasMore: !!ref.outgoing_has_more,
          incoming: ref.incoming_links ?? [],
          incomingHasMore: !!ref.incoming_has_more,
        });
        if (target === "detail") setDetailOpen(true);
        else setDeleteOpen(true);
      } catch (err) {
        if (pendingHydrationRef.current !== id) return;
        toast({
          variant: "destructive",
          title: t("hydrateFailed"),
          description: err instanceof Error ? err.message : undefined,
        });
      }
    },
    [items, toast, t],
  );

  const handleView = useCallback(
    (memory: Memory) => void openWith(memory.id, "detail"),
    [openWith],
  );

  const handleDelete = useCallback(
    (memory: Memory) => void openWith(memory.id, "delete"),
    [openWith],
  );

  const handleOpenLinkedMemory = useCallback(
    (id: string) => void openWith(id, "detail"),
    [openWith],
  );

  const handleDetailDelete = useCallback(() => {
    setDetailOpen(false);
    setDeleteOpen(true);
  }, []);

  const handleDeleteSuccess = useCallback(() => {
    setDeleteOpen(false);
    setDetailOpen(false);
    setHydrated(null);
    setLinkedRefs(EMPTY_LINKED_REFS);
    toast({ title: t("deleteSuccess") });

    // Avoid stranding the user on an empty page when the deleted row was the
    // last one on this page. Dropping `page` back triggers the fetch effect;
    // otherwise re-fetch the current page.
    if (items.length === 1 && page > 1) {
      setPage(page - 1);
    } else {
      void fetchMemories();
    }
  }, [fetchMemories, toast, t, items.length, page]);

  if (error) {
    return <ErrorBanner error={error} />;
  }

  if (!loading && items.length === 0) {
    return (
      <EmptyState
        icon={FileText}
        title={t("emptyTitle")}
        description={t("emptyDesc")}
      />
    );
  }

  const displayRows = items.map(listItemAsMemory);

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
      {hydrated && (
        <>
          <MemoryDetailDialog
            memory={hydrated}
            open={detailOpen}
            onOpenChange={setDetailOpen}
            onDelete={handleDetailDelete}
            outgoingLinks={linkedRefs.outgoing}
            outgoingHasMore={linkedRefs.outgoingHasMore}
            incomingLinks={linkedRefs.incoming}
            incomingHasMore={linkedRefs.incomingHasMore}
            onOpenLinkedMemory={handleOpenLinkedMemory}
          />
          <DeleteMemoryDialog
            memory={hydrated}
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            onSuccess={handleDeleteSuccess}
          />
        </>
      )}
    </>
  );
}
