"use client";

/**
 * Workspace Storage (File Objects) Page
 *
 * Lists files uploaded to platform R2 storage for the current workspace,
 * with download (viewer+) and delete (member+) actions. Wires the frontend
 * to the existing backend REST API (`backend/src/api/routes/files.py`).
 *
 * Issue #955
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations, useLocale } from "next-intl";
import { Download, HardDrive, Trash2 } from "lucide-react";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { TableLoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import {
  listFiles,
  getDownloadUrl,
  deleteFile,
  formatFileSize,
  type FileObject,
} from "@/lib/api/files";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useToast } from "@/hooks/use-toast";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";

const PAGE_SIZE = 50;
const MAX_LIMIT = 500;

export default function StoragePage() {
  const t = useTranslations("storage");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const { currentWorkspace, currentWorkspaceId } = useWorkspace();
  const { toast } = useToast();

  const [files, setFiles] = useState<FileObject[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(PAGE_SIZE);

  // Delete-confirmation state
  const [fileToDelete, setFileToDelete] = useState<FileObject | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);

  // member+ can delete; viewer is read-only (mirrors backend authz).
  const canDelete = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    WorkspaceRole.Member,
  );

  const fetchFiles = useCallback(
    async (nextLimit: number) => {
      if (!currentWorkspaceId) return;
      try {
        setLoading(true);
        setError(null);
        const data = await listFiles(currentWorkspaceId, nextLimit);
        setFiles(data);
      } catch (e) {
        setError(e instanceof Error ? e.message : t("list.fetchError"));
      } finally {
        setLoading(false);
      }
    },
    [currentWorkspaceId, t],
  );

  useEffect(() => {
    // Hold until WorkspaceContext hydrates so we don't fire a round-trip
    // with a null workspace id on first render.
    if (!currentWorkspaceId) return;
    fetchFiles(limit);
  }, [currentWorkspaceId, limit, fetchFiles]);

  useEffect(() => {
    document.title = `${t("list.title")} - Kagura Memory Cloud`;
  }, [t]);

  const handleDownload = async (f: FileObject) => {
    if (!currentWorkspaceId) return;
    try {
      setDownloadingId(f.id);
      const url = await getDownloadUrl(currentWorkspaceId, f.id);
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (e) {
      toast({
        variant: "destructive",
        title: t("list.actions.downloadFailed"),
        description: e instanceof Error ? e.message : t("list.fetchError"),
      });
    } finally {
      setDownloadingId(null);
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
      await fetchFiles(limit);
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

  // The backend returns no total count; offer "load more" only while the
  // page came back full and we have room under the server cap.
  const canLoadMore = files.length >= limit && limit < MAX_LIMIT;

  return (
    <PageContainer>
      <PageHeader title={t("list.title")} description={t("list.description")} />

      {error && <ErrorBanner error={error} />}

      {loading ? (
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
                    <TableCell className="text-right tabular-nums">
                      {formatFileSize(f.size_bytes)}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          f.status === "uploaded" ? "secondary" : "outline"
                        }
                      >
                        {f.status}
                      </Badge>
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
                          disabled={downloadingId === f.id}
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
                onClick={() =>
                  setLimit((n) => Math.min(n + PAGE_SIZE, MAX_LIMIT))
                }
              >
                {t("list.loadMore")}
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
    </PageContainer>
  );
}
