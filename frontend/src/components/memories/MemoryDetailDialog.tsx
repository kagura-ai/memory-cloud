/**
 * Memory Detail Dialog
 *
 * Displays detailed information about a memory. Receives ``memory`` plus
 * optional declared_link backlinks (Issue #440) and a ``notFound`` flag
 * (Issue #434) that lets the deep-link path render an EmptyState inside
 * the dialog when the URL pointed at an unreachable memory.
 */

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { Separator } from "@/components/ui/separator";
import {
  ArrowDownLeft,
  ArrowUpRight,
  Check,
  Copy,
  FileQuestion,
  GitMerge,
  Link2Off,
  Pencil,
  Trash2,
} from "lucide-react";
import { InlineSpinner } from "@/components/common/LoadingState";
import type {
  LinkedMemoryRef,
  MemoryReference,
  SupersedeCandidate,
} from "@/lib/types/memory";
import { formatDateTime } from "@/lib/utils/datetime";
import { useAuth } from "@/contexts/AuthContext";
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
import { useToast } from "@/hooks/use-toast";
import { useLocale, useTranslations } from "next-intl";

interface MemoryDetailDialogProps {
  // ``memory`` may be null in the deep-link "not found" path so the dialog
  // can still render an EmptyState (matches the contract in #434 — invalid
  // ``?memoryId=`` shows EmptyState inside the dialog, not a hard 404 page).
  memory: MemoryReference | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // Optional: omit to hide the Edit button entirely. Passing `undefined`
  // keeps the dialog honest — no ghost button when edit is unavailable.
  onEdit?: () => void;
  onDelete: () => void;
  // Issue #434: when ``referenceMemory`` rejects on a deep-link path, the
  // panel sets ``notFound=true`` so we render an EmptyState body instead of
  // closing or toasting. ``memory`` is null in this state.
  notFound?: boolean;
  // Issue #440: outgoing/incoming declared_link references. Optional —
  // omitting both (or passing empty arrays) hides the References section.
  outgoingLinks?: LinkedMemoryRef[];
  outgoingHasMore?: boolean;
  incomingLinks?: LinkedMemoryRef[];
  incomingHasMore?: boolean;
  onOpenLinkedMemory?: (memoryId: string) => void;
  // #1403/#1416: supersede suggestion + confirm action. Optional — omit (or
  // pass null) to hide the suggestion block. `onAcceptSupersede` POSTs the
  // `supersedes` edge; `supersedeAccepting` drives the confirm button's
  // pending state.
  supersedeCandidate?: SupersedeCandidate | null;
  onAcceptSupersede?: () => void;
  supersedeAccepting?: boolean;
}

export function MemoryDetailDialog({
  memory,
  open,
  onOpenChange,
  onEdit,
  onDelete,
  notFound = false,
  outgoingLinks,
  outgoingHasMore = false,
  incomingLinks,
  incomingHasMore = false,
  onOpenLinkedMemory,
  supersedeCandidate,
  onAcceptSupersede,
  supersedeAccepting = false,
}: MemoryDetailDialogProps) {
  const { user } = useAuth();
  const locale = useLocale();
  const t = useTranslations("contextDetail.detailDialog");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const { isCopied, copyToTarget } = useCopyFeedback();

  const copy = async (text: string, key: string) => {
    try {
      await copyToTarget(text, key);
    } catch (err) {
      // useCopyFeedback re-throws clipboard errors so callers can surface
      // them — frontend rule: button-driven mutation failures use toast.
      toast({
        variant: "destructive",
        title: tCommon("error"),
        description: err instanceof Error ? err.message : t("copyFailed"),
      });
    }
  };

  // NotFound state: memory is unreachable (deleted, cross-context, or the
  // user lacks permission). Render the shared <EmptyState> primitive inside
  // the dialog body so the empty/error UI matches every other surface in
  // the app (frontend rule: "MUST use EmptyState primitive"). Title +
  // description live in <DialogHeader className="sr-only"> to satisfy
  // Radix's accessibility requirement without visually duplicating the
  // EmptyState content.
  if (notFound || !memory) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-md">
          <DialogHeader className="sr-only">
            <DialogTitle>{t("notFoundTitle")}</DialogTitle>
            <DialogDescription>{t("notFoundDesc")}</DialogDescription>
          </DialogHeader>
          <EmptyState
            icon={FileQuestion}
            title={t("notFoundTitle")}
            description={t("notFoundDesc")}
            compact
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              {t("close")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  const hasOutgoing = (outgoingLinks?.length ?? 0) > 0;
  const hasIncoming = (incomingLinks?.length ?? 0) > 0;
  // "No references yet" fires only when BOTH directions resolved with zero
  // links. Partial fetches (one side ``[]``, the other ``undefined``) must
  // not claim "no incoming or outgoing" when one side was never queried.
  const referencesAreEmpty =
    outgoingLinks !== undefined &&
    incomingLinks !== undefined &&
    !hasOutgoing &&
    !hasIncoming;
  // Hide the section unless we have something honest to show — actual links
  // in either direction, or a confirmed empty state. Avoids rendering an
  // empty "References" header during partial fetches.
  const showReferences = hasOutgoing || hasIncoming || referencesAreEmpty;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {memory.summary}
            <Badge
              variant={memory.scope === "persistent" ? "default" : "outline"}
            >
              {memory.scope === "persistent"
                ? t("scopePersistent")
                : t("scopeWorking")}
            </Badge>
          </DialogTitle>
          <DialogDescription>{t("memoryDetails")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Value */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm font-medium">{t("value")}</label>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void copy(memory.content, "content")}
                className="h-8"
              >
                {isCopied("content") ? (
                  <>
                    <Check className="h-4 w-4 mr-1" />
                    {t("copied")}
                  </>
                ) : (
                  <>
                    <Copy className="h-4 w-4 mr-1" />
                    {t("copy")}
                  </>
                )}
              </Button>
            </div>
            <div className="p-3 bg-slate-50 dark:bg-slate-900 rounded-lg text-sm font-mono whitespace-pre-wrap break-words">
              {memory.content}
            </div>
          </div>

          <Separator />

          {/* Memory ID — full row with copy button */}
          <div>
            <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
              {t("memoryId")}
            </label>
            <div className="mt-1 flex items-center gap-2">
              <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded font-mono break-all">
                {memory.memory_id}
              </code>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void copy(memory.memory_id, "id")}
                className="h-7 shrink-0"
                title={t("copyMemoryId")}
                aria-label={t("copyMemoryId")}
              >
                {isCopied("id") ? (
                  <Check className="h-3.5 w-3.5" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
              </Button>
            </div>
          </div>

          {/* Metadata Grid — wrapper elements use <div> not <p> so the Badge
              children (which render as <div>) don't trigger the
              `<p> cannot contain <div>` HTML hydration warning. */}
          <div className="grid grid-cols-2 gap-4">
            {memory.type && (
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("type")}
                </label>
                <div className="mt-1">
                  <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded">
                    {memory.type}
                  </code>
                </div>
              </div>
            )}

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                {t("importance")}
              </label>
              <div className="mt-1">
                <Badge
                  variant={
                    memory.importance >= 0.8
                      ? "destructive"
                      : memory.importance >= 0.5
                        ? "default"
                        : "secondary"
                  }
                >
                  {memory.importance.toFixed(2)}
                </Badge>
              </div>
            </div>

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                {t("createdAt")}
              </label>
              <div className="mt-1 text-sm">
                {formatDateTime(memory.created_at, user?.timezone, locale)}
              </div>
            </div>

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                {t("updatedAt")}
              </label>
              <div className="mt-1 text-sm">
                {formatDateTime(memory.updated_at, user?.timezone, locale)}
              </div>
            </div>
          </div>

          {/* Source — origin URI + type for memories imported from a vault,
              file, URL, etc. (Issue #215). Hidden if memory has no origin. */}
          {memory.source_uri && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("source")}
                </label>
                <div className="mt-1 flex items-center gap-2">
                  {memory.source_type && (
                    <Badge variant="outline" className="shrink-0">
                      {memory.source_type}
                    </Badge>
                  )}
                  <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded break-all">
                    {memory.source_uri}
                  </code>
                </div>
              </div>
            </>
          )}

          {/* Supersede suggestion (#1403/#1416) — a near-duplicate this memory
              likely supersedes, surfaced for the user to confirm. Confirming
              POSTs a `supersedes` edge (self-healing the suggestion). This is an
              informational suggestion, not an error, so it uses a subtle
              highlighted block rather than ErrorBanner. */}
          {supersedeCandidate && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("supersede.title")}
                </label>
                <div className="mt-2 space-y-3 rounded-lg border border-amber-300/60 bg-amber-50 p-3 dark:border-amber-800/60 dark:bg-amber-950/30">
                  <p className="text-sm text-slate-700 dark:text-slate-300">
                    {t("supersede.description")}
                  </p>
                  <button
                    type="button"
                    onClick={() =>
                      onOpenLinkedMemory?.(supersedeCandidate.memory_id)
                    }
                    disabled={!onOpenLinkedMemory}
                    className="flex w-full items-center gap-2 rounded bg-white/70 px-2 py-1.5 text-left text-sm transition hover:bg-white disabled:cursor-default disabled:hover:bg-white/70 dark:bg-slate-900/40 dark:hover:bg-slate-900 dark:disabled:hover:bg-slate-900/40"
                  >
                    <span className="flex-1 truncate">
                      {supersedeCandidate.summary}
                    </span>
                    <code className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-xs dark:bg-slate-800">
                      {t("supersede.similarity", {
                        value: Math.round(supersedeCandidate.similarity * 100),
                      })}
                    </code>
                  </button>
                  <div className="flex justify-end">
                    <Button
                      size="sm"
                      onClick={onAcceptSupersede}
                      disabled={supersedeAccepting || !onAcceptSupersede}
                    >
                      {supersedeAccepting ? (
                        <InlineSpinner size="sm" className="mr-2" />
                      ) : (
                        <GitMerge className="mr-2 h-4 w-4" />
                      )}
                      {t("supersede.confirm")}
                    </Button>
                  </div>
                </div>
              </div>
            </>
          )}

          {/* References — declared_link backlinks. The list buttons compose
              with the deep-link path: the panel-side handler updates
              ``?memoryId=`` so the URL stays canonical when the user
              navigates between linked memories. */}
          {showReferences && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("references.title")}
                </label>
                <div className="mt-2 space-y-3">
                  {hasOutgoing && (
                    <ReferenceList
                      heading={t("references.outgoing")}
                      icon={<ArrowUpRight className="h-3.5 w-3.5" />}
                      links={outgoingLinks!}
                      hasMore={outgoingHasMore}
                      truncatedLabel={t("references.truncated")}
                      unknownLabel={t("references.unknown")}
                      onOpen={onOpenLinkedMemory}
                    />
                  )}
                  {hasIncoming && (
                    <ReferenceList
                      heading={t("references.incoming")}
                      icon={<ArrowDownLeft className="h-3.5 w-3.5" />}
                      links={incomingLinks!}
                      hasMore={incomingHasMore}
                      truncatedLabel={t("references.truncated")}
                      unknownLabel={t("references.unknown")}
                      onOpen={onOpenLinkedMemory}
                    />
                  )}
                  {referencesAreEmpty && (
                    <EmptyState
                      icon={Link2Off}
                      title={t("references.emptyTitle")}
                      description={t("references.emptyDesc")}
                      compact
                    />
                  )}
                </div>
              </div>
            </>
          )}

          {/* Tags */}
          {memory.tags && memory.tags.length > 0 && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("tags")}
                </label>
                <div className="flex flex-wrap gap-2 mt-2">
                  {memory.tags.map((tag) => (
                    <Badge key={tag} variant="outline">
                      {tag}
                    </Badge>
                  ))}
                </div>
              </div>
            </>
          )}

          {/* Metadata */}
          {memory.details && Object.keys(memory.details).length > 0 && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("metadata")}
                </label>
                <div className="mt-2 p-3 bg-slate-50 dark:bg-slate-900 rounded-lg text-sm font-mono">
                  <pre>{JSON.stringify(memory.details, null, 2)}</pre>
                </div>
              </div>
            </>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("close")}
          </Button>
          {onEdit && (
            <Button variant="outline" onClick={onEdit}>
              <Pencil className="h-4 w-4 mr-2" />
              {t("edit")}
            </Button>
          )}
          <Button variant="destructive" onClick={onDelete}>
            <Trash2 className="h-4 w-4 mr-2" />
            {t("delete")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface ReferenceListProps {
  heading: string;
  icon: React.ReactNode;
  links: LinkedMemoryRef[];
  hasMore: boolean;
  truncatedLabel: string;
  unknownLabel: string;
  onOpen?: (memoryId: string) => void;
}

function ReferenceList({
  heading,
  icon,
  links,
  hasMore,
  truncatedLabel,
  unknownLabel,
  onOpen,
}: ReferenceListProps) {
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-1.5 text-xs font-medium text-slate-600 dark:text-slate-300">
        {icon}
        <span>{heading}</span>
        <span className="text-slate-400">({links.length})</span>
      </div>
      <ul className="space-y-1">
        {links.map((link) => (
          <li key={link.memory_id}>
            <button
              type="button"
              onClick={() => onOpen?.(link.memory_id)}
              disabled={!onOpen}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-800 disabled:cursor-default disabled:hover:bg-transparent transition"
            >
              <span className="flex-1 truncate">
                {link.summary || unknownLabel}
              </span>
              {link.type && (
                <code className="text-xs bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 rounded shrink-0">
                  {link.type}
                </code>
              )}
            </button>
          </li>
        ))}
      </ul>
      {hasMore && (
        <p className="mt-1.5 text-xs text-slate-400 dark:text-slate-500 italic">
          {truncatedLabel}
        </p>
      )}
    </div>
  );
}
