"use client";

/**
 * useMemoryDetailDialog — shared dialog state machine for memory detail/edit/delete.
 *
 * Caller owns the URL param (`memoryIdParam` + `setMemoryIdParam`) and the
 * post-success side effects (toast, refetch, pagination) — those are invoked
 * AFTER `applyEditSuccess` / `applyDeleteSuccess` so this hook stays free of
 * UI dependencies.
 *
 * Hook owns dialog open state, hydrated MemoryReference + linked refs, and
 * two safety refs:
 *   - `pendingHydrationRef` — race guard, discards stale referenceMemory
 *     responses when the user clicks A then B before A resolves.
 *   - `skipNextDeepLinkEffectRef` — bypass token that stops the URL-driven
 *     effect from issuing a duplicate referenceMemory call when a click has
 *     already kicked one off.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { useTranslations } from "next-intl";
import { referenceMemory } from "@/lib/api/memory";
import type { LinkedMemoryRef, MemoryReference } from "@/lib/types/memory";

export type DialogTarget = "detail" | "delete" | "edit";

export interface LinkedRefsState {
  outgoing: LinkedMemoryRef[];
  outgoingHasMore: boolean;
  incoming: LinkedMemoryRef[];
  incomingHasMore: boolean;
}

export const EMPTY_LINKED_REFS: LinkedRefsState = {
  outgoing: [],
  outgoingHasMore: false,
  incoming: [],
  incomingHasMore: false,
};

function linkedRefsFromMemoryReference(ref: MemoryReference): LinkedRefsState {
  return {
    outgoing: ref.outgoing_links ?? [],
    outgoingHasMore: !!ref.outgoing_has_more,
    incoming: ref.incoming_links ?? [],
    incomingHasMore: !!ref.incoming_has_more,
  };
}

export interface UseMemoryDetailDialogOpts {
  memoryIdParam: string | null;
  setMemoryIdParam: (id: string | null) => void;
}

export interface UseMemoryDetailDialogResult {
  hydrated: MemoryReference | null;
  linkedRefs: LinkedRefsState;
  detailOpen: boolean;
  detailNotFound: boolean;
  deleteOpen: boolean;
  editOpen: boolean;

  // Dialog onOpenChange handlers — wire directly to MemoryDetailDialog /
  // EditMemoryDialog / DeleteMemoryDialog props of the same name.
  handleDetailOpenChange: (next: boolean) => void;
  handleEditOpenChange: (next: boolean) => void;
  handleDeleteOpenChange: (next: boolean) => void;

  // User-action triggers — call from row click / node click / backlink click.
  openDetail: (id: string) => void;
  openDelete: (id: string) => void;
  handleDetailEdit: () => void;
  handleDetailDelete: () => void;

  // Success appliers — caller invokes these inside its own success handler
  // (which also fires toasts, refetches lists, etc.).
  applyEditSuccess: (updated: MemoryReference) => void;
  applyDeleteSuccess: () => void;
}

export function useMemoryDetailDialog(
  opts: UseMemoryDetailDialogOpts,
): UseMemoryDetailDialogResult {
  const { memoryIdParam, setMemoryIdParam } = opts;
  const { toast } = useToast();
  const t = useTranslations("contextDetail.memoriesPanel");

  const [hydrated, setHydrated] = useState<MemoryReference | null>(null);
  const [linkedRefs, setLinkedRefs] =
    useState<LinkedRefsState>(EMPTY_LINKED_REFS);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailNotFound, setDetailNotFound] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);

  // Race guard: if the user clicks A then B before A's referenceMemory()
  // resolves, B sets pendingHydrationRef.current=B.id; when A resolves we
  // compare and discard. Cleared on dialog close so a stale in-flight
  // reference can't re-open the dialog after the user dismissed it.
  const pendingHydrationRef = useRef<string | null>(null);

  // Bypass token for the deep-link effect: openDetail mutates the URL AND
  // calls openWith() with viaUrl=false (so a failure surfaces as a toast).
  // Without this token the URL change would re-fire the effect, issue a
  // duplicate referenceMemory call, AND swap the toast path for the
  // viaUrl=true notFound path.
  const skipNextDeepLinkEffectRef = useRef<string | null>(null);

  // Tracks whether the delete dialog was opened FROM the detail dialog (via
  // handleDetailDelete) vs. directly (via openDelete from a list row). On
  // delete-cancel we only want to re-open detail in the first case —
  // re-opening detail after a row-delete cancel surprises the user with a
  // dialog they never explicitly opened.
  const deleteOpenedFromDetailRef = useRef(false);

  const openWith = useCallback(
    async (id: string, target: DialogTarget, viaUrl = false) => {
      pendingHydrationRef.current = id;
      try {
        const ref = await referenceMemory(id);
        if (pendingHydrationRef.current !== id) return;
        setHydrated(ref);
        setLinkedRefs(linkedRefsFromMemoryReference(ref));
        setDetailNotFound(false);
        if (target === "detail") setDetailOpen(true);
        else if (target === "edit") setEditOpen(true);
        else setDeleteOpen(true);
      } catch (err) {
        if (pendingHydrationRef.current !== id) return;
        if (viaUrl && target === "detail") {
          setHydrated(null);
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

  // Deep-link sync (Issue #434). See the long comment in the original
  // MemoriesTabPanel implementation for the rationale of the bypass token
  // and the null-branch reset of every dialog flag.
  useEffect(() => {
    if (!memoryIdParam) {
      setDetailOpen(false);
      setDetailNotFound(false);
      setHydrated(null);
      setLinkedRefs(EMPTY_LINKED_REFS);
      setEditOpen(false);
      setDeleteOpen(false);
      pendingHydrationRef.current = null;
      return;
    }
    if (skipNextDeepLinkEffectRef.current === memoryIdParam) {
      skipNextDeepLinkEffectRef.current = null;
      return;
    }
    if (hydrated?.memory_id === memoryIdParam && detailOpen) return;
    void openWith(memoryIdParam, "detail", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoryIdParam]);

  const handleDetailOpenChange = useCallback(
    (next: boolean) => {
      setDetailOpen(next);
      if (!next) {
        if (memoryIdParam) setMemoryIdParam(null);
        setDetailNotFound(false);
        // Cancel any in-flight referenceMemory: nulling the ref makes the
        // openWith resolution path's identity check fail, so a late-arriving
        // response can't re-open the dialog the user just closed.
        pendingHydrationRef.current = null;
      }
    },
    [memoryIdParam, setMemoryIdParam],
  );

  const openDetail = useCallback(
    (id: string) => {
      skipNextDeepLinkEffectRef.current = id;
      setMemoryIdParam(id);
      void openWith(id, "detail");
    },
    [openWith, setMemoryIdParam],
  );

  // Skip URL sync — the delete confirmation isn't a shareable state.
  const openDelete = useCallback(
    (id: string) => {
      deleteOpenedFromDetailRef.current = false;
      void openWith(id, "delete");
    },
    [openWith],
  );

  const handleDetailEdit = useCallback(() => {
    if (!hydrated) return;
    setEditOpen(true);
  }, [hydrated]);

  const handleDetailDelete = useCallback(() => {
    deleteOpenedFromDetailRef.current = true;
    setDetailOpen(false);
    setDeleteOpen(true);
  }, []);

  const handleEditOpenChange = useCallback((next: boolean) => {
    setEditOpen(next);
  }, []);

  const handleDeleteOpenChange = useCallback(
    (next: boolean) => {
      setDeleteOpen(next);
      if (!next) {
        // Cancel path: re-open detail ONLY when delete was launched from
        // the detail dialog itself. Direct row-deletes (openDelete) should
        // close cleanly without bouncing the user into a detail view they
        // never asked for. On the success path, applyDeleteSuccess clears
        // `hydrated` first so this branch is skipped regardless.
        if (hydrated && deleteOpenedFromDetailRef.current) {
          setDetailOpen(true);
        }
        deleteOpenedFromDetailRef.current = false;
      }
    },
    [hydrated],
  );

  const applyEditSuccess = useCallback((updated: MemoryReference) => {
    setHydrated(updated);
    setLinkedRefs(linkedRefsFromMemoryReference(updated));
    setEditOpen(false);
  }, []);

  const applyDeleteSuccess = useCallback(() => {
    setDeleteOpen(false);
    setDetailOpen(false);
    setHydrated(null);
    setLinkedRefs(EMPTY_LINKED_REFS);
    if (memoryIdParam) setMemoryIdParam(null);
  }, [memoryIdParam, setMemoryIdParam]);

  return {
    hydrated,
    linkedRefs,
    detailOpen,
    detailNotFound,
    deleteOpen,
    editOpen,
    handleDetailOpenChange,
    handleEditOpenChange,
    handleDeleteOpenChange,
    openDetail,
    openDelete,
    handleDetailEdit,
    handleDetailDelete,
    applyEditSuccess,
    applyDeleteSuccess,
  };
}
