/**
 * Memories Table Component
 *
 * Displays memories in a table with pagination
 */

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
import { Eye, FileText, Pencil, Trash2 } from "lucide-react";
import type { MemoryListItem } from "@/lib/types/memory";
import { formatRelativeTime } from "@/lib/utils/datetime";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { useLocale, useTranslations } from "next-intl";

interface MemoriesTableProps {
  memories: MemoryListItem[];
  loading: boolean;
  onView: (memory: MemoryListItem) => void;
  // Optional: omit to hide the Edit icon. Same convention as MemoryDetailDialog
  // — passing nothing keeps the dialog honest (no ghost button).
  onEdit?: (memory: MemoryListItem) => void;
  onDelete: (memory: MemoryListItem) => void;
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
}

export function MemoriesTable({
  memories,
  loading,
  onView,
  onEdit,
  onDelete,
  page,
  pageSize,
  total,
  onPageChange,
}: MemoriesTableProps) {
  const locale = useLocale();
  const t = useTranslations("contextDetail.memoriesTable");
  const totalPages = Math.ceil(total / pageSize);

  const getImportanceBadge = (importance: number) => {
    if (importance >= 0.8)
      return <Badge variant="destructive">{t("importanceHigh")}</Badge>;
    if (importance >= 0.5)
      return <Badge variant="default">{t("importanceMedium")}</Badge>;
    return <Badge variant="secondary">{t("importanceLow")}</Badge>;
  };

  const getScopeBadge = (scope: string) => {
    return scope === "persistent" ? (
      <Badge variant="default">{t("scopePersistent")}</Badge>
    ) : (
      <Badge variant="outline">{t("scopeWorking")}</Badge>
    );
  };

  if (loading && memories.length === 0) {
    return (
      <div className="flex items-center justify-center h-64">
        <SpinnerLoading size="md" message={t("loading")} />
      </div>
    );
  }

  if (!loading && memories.length === 0) {
    return (
      <EmptyState
        icon={FileText}
        title={t("emptyTitle")}
        description={t("emptyDesc")}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-slate-200 dark:border-slate-800">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("columnSummary")}</TableHead>
              <TableHead>{t("columnType")}</TableHead>
              <TableHead>{t("columnScope")}</TableHead>
              <TableHead>{t("columnImportance")}</TableHead>
              <TableHead>{t("columnUpdated")}</TableHead>
              <TableHead className="text-right">{t("columnActions")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {memories.map((memory) => (
              <TableRow key={memory.id}>
                <TableCell className="font-medium max-w-xs truncate">
                  {memory.summary}
                </TableCell>
                <TableCell>
                  {memory.type ? (
                    <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded">
                      {memory.type}
                    </code>
                  ) : (
                    <span className="text-xs text-slate-400">—</span>
                  )}
                </TableCell>
                <TableCell>{getScopeBadge(memory.scope)}</TableCell>
                <TableCell>{getImportanceBadge(memory.importance)}</TableCell>
                <TableCell className="text-sm text-slate-500">
                  {formatRelativeTime(memory.updated_at, locale)}
                </TableCell>
                <TableCell className="text-right">
                  <div className="flex items-center justify-end gap-2">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => onView(memory)}
                      title={t("actionView")}
                      aria-label={t("actionView")}
                    >
                      <Eye className="h-4 w-4" />
                    </Button>
                    {onEdit && (
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => onEdit(memory)}
                        title={t("actionEdit")}
                        aria-label={t("actionEdit")}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => onDelete(memory)}
                      title={t("actionDelete")}
                      aria-label={t("actionDelete")}
                      className="text-red-600 hover:text-red-700 hover:bg-red-50 dark:hover:bg-red-900/20"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-slate-500">
            {t("paginationShowing", {
              start: (page - 1) * pageSize + 1,
              end: Math.min(page * pageSize, total),
              total,
            })}
          </p>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onPageChange(page - 1)}
              disabled={page === 1 || loading}
            >
              {t("paginationPrevious")}
            </Button>
            <span className="text-sm text-slate-700 dark:text-slate-300">
              {t("paginationPage", { page, totalPages })}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => onPageChange(page + 1)}
              disabled={page === totalPages || loading}
            >
              {t("paginationNext")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
