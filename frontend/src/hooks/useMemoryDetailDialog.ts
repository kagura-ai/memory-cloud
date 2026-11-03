"use client";

/**
 * useMemoryDetailDialog — shared dialog state machine for memory detail/edit/delete.
 *
 * Caller owns the URL param (`memoryIdParam` + `setMemoryIdParam`) and the
 * post-success side effects (toast, refetch, pagination) — those are invoked
 * AFTER `applyEditSuccess` / `applyDeleteSuccess`, or from the optional
 * `onSupersedeAccepted` callback, so this hook stays free of UI dependencies.
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
import { referenceMemory, updateMemoryById } from "@/lib/api/memory";
import { ApiError } from "@/lib/api/base";
import type {
  LinkedMemoryRef,
  MemoryReference,
  SupersedeCandidate,
} from "@/lib/types/memory";

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

// #2427: a failed accept, shown inline in the suggestion block (a dialog-body
// error) rather than as a toast. `description` carries the server's text only
// for an unmapped failure. `blocksRetry` (a 409: another declared edge links
// the pair — one declared after the memory was read, since one already there
// is `acceptance.reason === "edge_exists"`, #2518) keeps the action disabled
// until the dialog shows another memory, so the click cannot repeat the
// conflict.
export interface SupersedeError {
  title: string;
  description?: string;
  blocksRetry?: boolean;
}

// #2515: the pair a successful accept linked — `sourceId` (the newer memory,
// the one the dialog shows) now supersedes `targetId`.
export interface AcceptedSupersede {
  sourceId: string;
  targetId: string;
}

export interface UseMemoryDetailDialogOpts {
  memoryIdParam: string | null;
  setMemoryIdParam: (id: string | null) => void;
  // #2515: called after a successful accept, once the hook has applied its
  // own state, so the caller can refresh rows it caches (the Memories tab's
  // recall mode). Not called for a failed or refused accept. Fires even if
  // the dialog moved on meanwhile: the edge exists either way.
  onSupersedeAccepted?: (accepted: AcceptedSupersede) => void;
}

export interface UseMemoryDetailDialogResult {
  hydrated: MemoryReference | null;
  linkedRefs: LinkedRefsState;
  detailOpen: boolean;
  detailNotFound: boolean;
  deleteOpen: boolean;
  editOpen: boolean;

  // #1403/#1416: the current memory's supersede suggestion (null once accepted
  // this session or when there is none), a pending flag for the confirm button,
  // and the accept action (#2600: the memory's PATCH with
  // `accept_supersede_candidate`, which writes the `supersedes` edge and clears
  // the suggestion server-side).
  supersedeCandidate: SupersedeCandidate | null;
  supersedeAccepting: boolean;
  acceptSupersede: () => void;
  // #2427: the last accept failure for the open memory (null when none).
  supersedeError: SupersedeError | null;

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
  const { memoryIdParam, setMemoryIdParam, onSupersedeAccepted } = opts;
  const { toast } = useToast();
  const t = useTranslations("contextDetail.memoriesPanel");
  const tDetail = useTranslations("contextDetail.detailDialog");

  const [hydrated, setHydrated] = useState<MemoryReference | null>(null);
  const [linkedRefs, setLinkedRefs] =
    useState<LinkedRefsState>(EMPTY_LINKED_REFS);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailNotFound, setDetailNotFound] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [supersedeError, setSupersedeError] = useState<SupersedeError | null>(
    null,
  );

  // Race guard: if the user clicks A then B before A's referenceMemory()
  // resolves, B sets pendingHydrationRef.current=B.id; when A resolves we
  // compare and discard. Cleared on dialog close so a stale in-flight
  // reference can't re-open the dialog after the user dismissed it.
  const pendingHydrationRef = useRef<string | null>(null);

  // #2427: generation of the dialog's view of the memory. Bumped by every new
  // hydration (openWith), an edit's PATCH reply and closing the dialog, so a
  // late accept failure or re-read for an older view is dropped even when the
  // memory id is the same (close + reopen, or an edit saved meanwhile).
  const viewGenerationRef = useRef(0);

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
      viewGenerationRef.current += 1;
      try {
        const ref = await referenceMemory(id);
        if (pendingHydrationRef.current !== id) return;
        setHydrated(ref);
        setLinkedRefs(linkedRefsFromMemoryReference(ref));
        setDetailNotFound(false);
        setSupersedeError(null);
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
      setSupersedeError(null);
      pendingHydrationRef.current = null;
      viewGenerationRef.current += 1;
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
        setSupersedeError(null);
        // Cancel any in-flight referenceMemory: nulling the ref makes the
        // openWith resolution path's identity check fail, so a late-arriving
        // response can't re-open the dialog the user just closed.
        pendingHydrationRef.current = null;
        viewGenerationRef.current += 1;
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
    viewGenerationRef.current += 1;
    setHydrated(updated);
    setLinkedRefs(linkedRefsFromMemoryReference(updated));
    setEditOpen(false);
    // The PATCH reply carries the suggestion's current state (#2427).
    setSupersedeError(null);
  }, []);

  const applyDeleteSuccess = useCallback(() => {
    viewGenerationRef.current += 1;
    setDeleteOpen(false);
    setDetailOpen(false);
    setHydrated(null);
    setLinkedRefs(EMPTY_LINKED_REFS);
    if (memoryIdParam) setMemoryIdParam(null);
  }, [memoryIdParam, setMemoryIdParam]);

  // #1403/#1416: supersede suggestion + confirm→accept flow (#2600: the
  // accept of MCP update_memory, through the memory's PATCH).
  const [supersedeAccepting, setSupersedeAccepting] = useState(false);
  // Key of the (source→target) suggestion accepted in this session, so the
  // block hides immediately on success without waiting for a re-fetch (the
  // server also self-heals the stored suggestion once the edge exists).
  const [acceptedSupersedeKey, setAcceptedSupersedeKey] = useState<
    string | null
  >(null);

  const rawSupersedeCandidate = hydrated?.supersede_candidate ?? null;
  const supersedeKey =
    hydrated && rawSupersedeCandidate
      ? `${hydrated.memory_id}->${rawSupersedeCandidate.memory_id}`
      : null;
  const supersedeCandidate =
    rawSupersedeCandidate && supersedeKey !== acceptedSupersedeKey
      ? rawSupersedeCandidate
      : null;

  // #2427: re-read the open memory after a refused accept so the block shows
  // the server's current answer (a candidate gone, or `acceptance` now
  // refusing). `stale` when the view changed meanwhile (another memory, an
  // edit's reply, a close): nothing is applied then. `ref: null` when the
  // re-read failed: the dialog keeps its view and the caller still reports
  // the refusal. #2518: the re-read is not an adoption read, so it records no
  // access stats (the dialog's first read of the memory does).
  const refreshHydrated = useCallback(
    async (
      id: string,
      generation: number,
    ): Promise<
      { stale: true } | { stale: false; ref: MemoryReference | null }
    > => {
      let ref: MemoryReference | null = null;
      try {
        ref = await referenceMemory(id, { recordAccess: false });
      } catch {
        ref = null;
      }
      if (
        viewGenerationRef.current !== generation ||
        pendingHydrationRef.current !== id
      ) {
        return { stale: true };
      }
      if (ref) {
        setHydrated(ref);
        setLinkedRefs(linkedRefsFromMemoryReference(ref));
      }
      return { stale: false, ref };
    },
    [],
  );

  const acceptSupersede = useCallback(() => {
    if (!hydrated || !rawSupersedeCandidate) return;
    // #2427: the server said this caller may not accept it — including
    // `edge_exists` (#2518), the 409 the click would meet; the button is
    // disabled too, this guards any other caller of the action.
    if (rawSupersedeCandidate.acceptance?.allowed === false) return;
    const sourceId = hydrated.memory_id;
    const targetId = rawSupersedeCandidate.memory_id;
    const generation = viewGenerationRef.current;
    const viewIsCurrent = () =>
      viewGenerationRef.current === generation &&
      pendingHydrationRef.current === sourceId;
    setSupersedeAccepting(true);
    setSupersedeError(null);
    void (async () => {
      let accepted = false;
      try {
        // #2600: MCP update_memory's accept, so the edge is kept under the
        // memory's author (in a private context possibly an account linked
        // to the caller), where supersede detection looks for it. The server
        // accepts the suggestion as it stands, under the row lock.
        const updated = await updateMemoryById(sourceId, {
          accept_supersede_candidate: true,
        });
        accepted = true;
        setAcceptedSupersedeKey(`${sourceId}->${targetId}`);
        // The reply is the memory as it now stands (the suggestion cleared,
        // the new link listed), for the view that sent the accept only.
        if (viewIsCurrent()) {
          setHydrated(updated);
          setLinkedRefs(linkedRefsFromMemoryReference(updated));
        }
        toast({ title: tDetail("supersede.accepted") });
      } catch (err) {
        // A reply for a view the dialog no longer shows is dropped.
        if (!viewIsCurrent()) return;
        // Status, not server prose: 403 an endpoint no longer the caller's
        // own when the edge was written (the copy names nothing), 404 the
        // memory or write access gone (the route's deny is a uniform 404),
        // 410 the memory forgotten, 409 another declared edge on the pair,
        // 422 an accept the server refused (no live suggestion, not the
        // caller's own, a newer candidate). Each re-reads the memory first,
        // so the message and the block follow the server's current answer.
        const status = err instanceof ApiError ? err.status : undefined;
        if (
          status === 403 ||
          status === 404 ||
          status === 409 ||
          status === 410 ||
          status === 422
        ) {
          const refreshed = await refreshHydrated(sourceId, generation);
          if (refreshed.stale) return;
          let title: string;
          if (status === 409) {
            title = tDetail("supersede.errors.conflict");
          } else if (
            status !== 403 &&
            refreshed.ref?.supersede_candidate?.memory_id !== targetId
          ) {
            // The suggestion is gone or names another memory now (or the
            // memory could not be re-read).
            title = tDetail("supersede.errors.stale");
          } else {
            // A 403, or a refusal while the suggestion still surfaces — e.g.
            // a member downgraded to viewer, whose refusal the route keeps a
            // uniform 404, or a 422 for a pair that is not the caller's own;
            // the refreshed `acceptance` disables the button.
            title = tDetail("supersede.errors.denied");
          }
          setSupersedeError({
            title,
            ...(status === 409 ? { blocksRetry: true } : {}),
          });
        } else {
          setSupersedeError({
            title: tDetail("supersede.acceptFailed"),
            description: err instanceof Error ? err.message : undefined,
          });
        }
      } finally {
        setSupersedeAccepting(false);
      }
      // #2515: outside the try, so a throw in the caller's handler is never
      // reported as a failed accept.
      if (accepted) onSupersedeAccepted?.({ sourceId, targetId });
    })();
  }, [
    hydrated,
    rawSupersedeCandidate,
    toast,
    tDetail,
    refreshHydrated,
    onSupersedeAccepted,
  ]);

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
    supersedeCandidate,
    supersedeAccepting,
    acceptSupersede,
    supersedeError,
  };
}
