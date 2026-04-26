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
 * Create is still deferred — needs an MCP-shape form rewrite.
 */

"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { FileText } from "lucide-react";
import { MemoriesTable } from "@/components/memories/MemoriesTable";
import { MemoryDetailDialog } from "@/components/memories/MemoryDetailDialog";
import { DeleteMemoryDialog } from "@/components/memories/DeleteMemoryDialog";
import { EditMemoryDialog } from "@/components/memories/EditMemoryDialog";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import { useToast } from "@/hooks/use-toast";
import { useMemoryDetailDialog } from "@/hooks/useMemoryDetailDialog";
import { useMemoryIdParam } from "@/hooks/useMemoryIdParam";
import { getMemories } from "@/lib/api/memory";
import type { MemoryListItem, MemoryReference } from "@/lib/types/memory";

interface MemoriesTabPanelProps {
  contextId: string;
}

const PAGE_SIZE = 50;

export function MemoriesTabPanel({ contextId }: MemoriesTabPanelProps) {
  const t = useTranslations("contextDetail.memoriesPanel");
  const { toast } = useToast();
  const [memoryIdParam, setMemoryIdParam] = useMemoryIdParam();

  const [items, setItems] = useState<MemoryListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const dialog = useMemoryDetailDialog({ memoryIdParam, setMemoryIdParam });

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
        memories={items}
        loading={loading}
        onView={handleView}
        onDelete={handleDelete}
        page={page}
        pageSize={PAGE_SIZE}
        total={total}
        onPageChange={setPage}
      />
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
        />
      )}
    </>
  );
}
