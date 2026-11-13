"use client";

/**
 * Workspace Storage (File Objects) Page
 *
 * Lists files uploaded to platform R2 storage for the current workspace,
 * with download (viewer+) and delete (member+) actions. Wires the frontend
 * to the existing backend REST API (`backend/src/api/routes/files.py`).
 *
 * Issue #955. #565: malware scan state per file (badge), no download for a
 * blocked file, and the download refusals while a scan holds a file.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations, useLocale } from "next-intl";
import { Download, FolderMinus, HardDrive, Trash2 } from "lucide-react";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { TableLoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { formatRelativeTime } from "@/lib/utils/datetime";
import { formatBytes } from "@/lib/utils/format";
import {
  listFiles,
  getDownloadUrl,
  deleteContextFiles,
  deleteFile,
  isScanBlocked,
  SCAN_BLOCKED_ERROR,
  SCAN_PENDING_ERROR,
  type FileObject,
} from "@/lib/api/files";
import { ApiError } from "@/lib/api/base";
import { getContexts } from "@/lib/api/contexts";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useToast } from "@/hooks/use-toast";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";

const PAGE_SIZE = 50;
const MAX_LIMIT = 500;

export default function StoragePage() {
  const { currentWorkspaceId } = useWorkspace();
  return <StorageContent key={currentWorkspaceId ?? "loading"} />;
}

function StorageContent() {
  const t = useTranslations("storage");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const { currentWorkspace, currentWorkspaceId } = useWorkspace();
  const { toast } = useToast();

  const [files, setFiles] = useState<FileObject[]>([]);
  const [selectedScope, setSelectedScope] = useState("workspace");
  const requestId = useRef(0);
  const cancelPending = useCallback(() => { requestId.current++; }, []);
  // Accessible contexts provide both scope choices and display names.
  const [contextNames, setContextNames] = useState<Record<string, string>>({});
  const [contextsFailed, setContextsFailed] = useState(false);
  const [contextRetry, setContextRetry] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(PAGE_SIZE);

  // Delete-confirmation state
  const [fileToDelete, setFileToDelete] = useState<FileObject | null>(null);
  const [deleting, setDeleting] = useState(false);
  // #1960: "delete every file of this context" (the default context cannot be
  // deleted, and the capacity lock counts file bytes).
  const [contextToClear, setContextToClear] = useState<{
    id: string;
    name: string;
  } | null>(null);
  const [clearing, setClearing] = useState(false);
  // Shown inside the bulk-delete dialog: a toast behind the modal is easy to miss.
  const [clearError, setClearError] = useState<string | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);

  // member+ can delete; viewer is read-only (mirrors backend authz).
  const canDelete = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    WorkspaceRole.Member,
  );
  // Clearing a whole context's files needs the context's owner role on the
  // backend (#1960); workspace owner/admin hold it on every context, a plain
  // member only on contexts they created, which this page cannot tell apart.
  const canClearContext = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    WorkspaceRole.Admin,
  );

  const loadFiles = useCallback(
    async (nextLimit: number, mode: "initial" | "more" | "refresh") => {
      if (!currentWorkspaceId) return;
      const request = ++requestId.current;
      if (mode === "initial") {
        setLoading(true);
        setError(null);
      } else if (mode === "more") {
        setLoadingMore(true);
      }
      try {
        const data = await listFiles(currentWorkspaceId, nextLimit,
          selectedScope === "workspace" ? { scope: "workspace" } : { contextId: selectedScope });
        if (request !== requestId.current) return;
        setFiles(data);
        setLimit(nextLimit);
      } catch (e) {
        if (request !== requestId.current) return;
        const message = e instanceof Error ? e.message : t("list.fetchError");
        if (mode === "initial") {
          // First paint failed — there are no rows to protect, so surface a
          // page-level banner.
          setError(message);
        } else {
          // A "load more" or post-delete refresh failed. These are user
          // actions, so per the Error Surface rule we toast (and keep the
          // already-loaded rows on screen) instead of replacing the table
          // with a banner.
          toast({
            variant: "destructive",
            title: t("list.loadMoreFailed"),
            description: message,
          });
        }
      } finally {
        if (request === requestId.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [currentWorkspaceId, selectedScope, t, toast],
  );

  useEffect(() => {
    // Hold until WorkspaceContext hydrates so we don't fire a round-trip
    // with a null workspace id on first render.
    if (!currentWorkspaceId) return;
    void loadFiles(PAGE_SIZE, "initial");
    return cancelPending;
  }, [currentWorkspaceId, loadFiles, cancelPending]);

  useEffect(() => {
    // A failed lookup must be recoverable: these are the context scope choices.
    if (!currentWorkspaceId) return;
    let cancelled = false;
    getContexts()
      .then((res) => {
        if (cancelled) return;
        const map: Record<string, string> = {};
        // `||` (not `??`) so an empty-string display_name falls back to the name.
        for (const c of res.contexts) map[c.id] = c.display_name || c.name;
        setContextNames(map);
        setContextsFailed(false);
      })
      .catch(() => {
        if (!cancelled) setContextsFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId, contextRetry]);

  useEffect(() => {
    document.title = `${t("list.title")} - Kagura Memory Cloud`;
  }, [t]);

  const handleDownload = async (f: FileObject) => {
    if (!currentWorkspaceId) return;
    // Open the tab synchronously while we still hold the click's user
    // activation — calling window.open() after the await would be blocked by
    // popup blockers (Safari / strict Firefox). We can't pass "noopener" here
    // (that makes window.open return null and we'd lose the handle), so we
    // sever the opener manually before navigating to mitigate tabnabbing.
    const tab = window.open("about:blank", "_blank");
    if (tab) {
      // Some runtimes (e.g. jsdom) expose `opener` as a getter-only property;
      // ignore the failure there — in real browsers this severs the link.
      try {
        tab.opener = null;
      } catch {
        /* opener is read-only in this environment */
      }
    }
    try {
      setDownloadingId(f.id);
      const url = await getDownloadUrl(currentWorkspaceId, f.id);
      if (tab) {
        tab.location.replace(url);
      } else {
        // Popup blocked outright — fall back to a same-tab navigation.
        window.location.href = url;
      }
    } catch (e) {
      tab?.close();
      const code = e instanceof ApiError ? e.error : undefined;
      if (code === SCAN_PENDING_ERROR || code === SCAN_BLOCKED_ERROR) {
        // #565: the scan holds the file. Our own text (not the server's), and
        // a refresh so the row shows its current scan state.
        toast(
          code === SCAN_PENDING_ERROR
            ? { title: t("list.scan.downloadPending") }
            : { variant: "destructive", title: t("list.scan.downloadBlocked") },
        );
        void loadFiles(limit, "refresh");
        return;
      }
      toast({
        variant: "destructive",
        title: t("list.actions.downloadFailed"),
        description: e instanceof Error ? e.message : t("list.fetchError"),
      });
    } finally {
      setDownloadingId(null);
    }
  };

  // #565: the scan state badge; none for a downloadable file.
  const scanBadge = (f: FileObject) => {
    switch (f.scan_status) {
      case "pending":
        return (
          <Badge variant="outline" title={t("list.scan.pendingHint")}>
            {t("list.scan.pending")}
          </Badge>
        );
      case "infected":
        return (
          <Badge variant="destructive" title={t("list.scan.blockedHint")}>
            {t("list.scan.blocked")}
          </Badge>
        );
      case "error":
        return (
          <Badge variant="destructive" title={t("list.scan.errorHint")}>
            {t("list.scan.error")}
          </Badge>
        );
      default:
        return null;
    }
  };

  const handleConfirmDelete = async () => {
    if (!currentWorkspaceId || !fileToDelete) return;
    try {
      setDeleting(true);
      await deleteFile(currentWorkspaceId, fileToDelete.id);
      toast({
        title: t("list.deleteDialog.success"),
        description: fileToDelete.filename,
      });
      setFileToDelete(null);
      await loadFiles(limit, "refresh");
    } catch (e) {
      toast({
        variant: "destructive",
        title: t("list.deleteDialog.failed"),
        description: e instanceof Error ? e.message : t("list.fetchError"),
      });
    } finally {
      setDeleting(false);
    }
  };

  const handleConfirmClearContext = async () => {
    if (!contextToClear) return;
    try {
      setClearing(true);
      setClearError(null);
      const result = await deleteContextFiles(contextToClear.id);
      toast({
        title: t("list.deleteContextDialog.success", { count: result.deleted }),
        description: contextToClear.name,
      });
      setContextToClear(null);
      await loadFiles(limit, "refresh");
    } catch (e) {
      setClearError(e instanceof Error ? e.message : t("list.fetchError"));
    } finally {
      setClearing(false);
    }
  };

  // The backend returns no total count; offer "load more" only while the
  // page came back full and we have room under the server cap.
  const canLoadMore = files.length >= limit && limit < MAX_LIMIT;

  return (
    <PageContainer>
      <PageHeader title={t("list.title")} description={t("list.description")} />
      <div className="space-y-2">
        <Select value={selectedScope} onValueChange={value => {
          cancelPending();
          setFiles([]);
          setError(null);
          setFileToDelete(null);
          setContextToClear(null);
          setClearError(null);
          setLoading(true);
          setLoadingMore(false);
          setSelectedScope(value);
        }} disabled={deleting || clearing}>
          <SelectTrigger className="w-full sm:w-72" aria-label={t("list.filter.label")}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="workspace">{t("list.filter.workspace")}</SelectItem>
            {Object.entries(contextNames).map(([id, name]) => <SelectItem key={id} value={id}>{name}</SelectItem>)}
          </SelectContent>
        </Select>
        <p className="text-sm text-muted-foreground">
          {t(selectedScope === "workspace" ? "list.filter.sharedHint" : "list.filter.contextHint")}
        </p>
      </div>

      {contextsFailed && (
        <Alert variant="destructive">
          <AlertDescription className="flex flex-wrap items-center gap-3">
            {t("list.filter.fetchError")}
            <Button variant="outline" size="sm" onClick={() => { setContextsFailed(false); setContextRetry(value => value + 1); }}>
              {tCommon("retry")}
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {error && <ErrorBanner error={error} />}

      {loading && files.length === 0 ? (
        // Full skeleton only on first paint. "Load more" refetches with a
        // larger limit, so once rows exist we keep them visible (and disable
        // the button) instead of wiping the table back to a skeleton.
        <TableLoadingState rows={5} />
      ) : error ? null : files.length === 0 ? (
        <EmptyState
          icon={HardDrive}
          title={t("list.emptyTitle")}
          description={t("list.emptyDescription")}
        />
      ) : (
        <>
          <div className="rounded-lg border bg-card">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("list.column.filename")}</TableHead>
                  <TableHead>{t("list.column.type")}</TableHead>
                  <TableHead>{t("list.column.scope")}</TableHead>
                  <TableHead className="text-right">
                    {t("list.column.size")}
                  </TableHead>
                  <TableHead>{t("list.column.status")}</TableHead>
                  <TableHead>{t("list.column.uploaded")}</TableHead>
                  <TableHead
                    className="text-right"
                    aria-label={tCommon("actions")}
                  />
                </TableRow>
              </TableHeader>
              <TableBody>
                {files.map((f) => (
                  <TableRow key={f.id}>
                    <TableCell className="font-medium">{f.filename}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {f.content_type}
                    </TableCell>
                    <TableCell>
                      {f.context_id ? (
                        <Badge variant="outline" title={f.context_id}>
                          {contextNames[f.context_id] ??
                            `${f.context_id.slice(0, 8)}…`}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground text-sm">
                          {t("list.scope.workspace")}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatBytes(f.size_bytes)}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-1">
                        <Badge
                          variant={
                            f.status === "uploaded" ? "secondary" : "outline"
                          }
                        >
                          {f.status}
                        </Badge>
                        {scanBadge(f)}
                      </div>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-sm">
                      {f.uploaded_at ? (
                        formatRelativeTime(f.uploaded_at, locale)
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDownload(f)}
                          disabled={downloadingId === f.id || isScanBlocked(f)}
                          aria-label={t("list.actions.download")}
                          title={t("list.actions.download")}
                        >
                          <Download className="h-4 w-4" aria-hidden="true" />
                        </Button>
                        {canDelete && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setFileToDelete(f)}
                            aria-label={t("list.actions.delete")}
                            title={t("list.actions.delete")}
                            className="text-destructive hover:text-destructive"
                          >
                            <Trash2 className="h-4 w-4" aria-hidden="true" />
                          </Button>
                        )}
                        {canClearContext && f.context_id && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => {
                              const ctxId = f.context_id;
                              if (!ctxId) return;
                              setContextToClear({
                                id: ctxId,
                                name:
                                  contextNames[ctxId] ??
                                  `${ctxId.slice(0, 8)}…`,
                              });
                            }}
                            aria-label={t("list.actions.deleteContextFiles")}
                            title={t("list.actions.deleteContextFiles")}
                            className="text-destructive hover:text-destructive"
                          >
                            <FolderMinus
                              className="h-4 w-4"
                              aria-hidden="true"
                            />
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          {canLoadMore && (
            <div className="mt-4 flex justify-center">
              <Button
                variant="outline"
                disabled={loadingMore}
                onClick={() =>
                  loadFiles(Math.min(limit + PAGE_SIZE, MAX_LIMIT), "more")
                }
              >
                {loadingMore ? t("list.loadingMore") : t("list.loadMore")}
              </Button>
            </div>
          )}
        </>
      )}

      <AlertDialog
        open={fileToDelete !== null}
        onOpenChange={(open) => {
          if (!open) setFileToDelete(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("list.deleteDialog.title")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("list.deleteDialog.description", {
                filename: fileToDelete?.filename ?? "",
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                // Keep the dialog mounted through the async delete; we close
                // it ourselves on success so the spinner stays visible.
                e.preventDefault();
                handleConfirmDelete();
              }}
              disabled={deleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleting
                ? t("list.deleteDialog.deleting")
                : t("list.deleteDialog.confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={contextToClear !== null}
        onOpenChange={(open) => {
          if (!open) {
            setContextToClear(null);
            setClearError(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t("list.deleteContextDialog.title")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("list.deleteContextDialog.description", {
                context: contextToClear?.name ?? "",
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {clearError && (
            <Alert variant="destructive" role="alert">
              <AlertDescription>
                {t("list.deleteContextDialog.failed")} {clearError}
              </AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={clearing}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault();
                handleConfirmClearContext();
              }}
              disabled={clearing}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {clearing
                ? t("list.deleteContextDialog.deleting")
                : t("list.deleteContextDialog.confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}
