/**
 * Memories Table Component
 *
 * Displays memories in a table with pagination
 * Issue #666: Phase 2 - Added bulk delete checkbox support
 */

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Eye, Pencil, Trash2 } from "lucide-react";
import type { Memory } from "@/lib/types/memory";
import { formatRelativeTime } from "@/lib/utils/datetime";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { useAuth } from "@/contexts/AuthContext";
import { useLocale } from "next-intl";

interface MemoriesTableProps {
  memories: Memory[];
  loading: boolean;
  onView: (memory: Memory) => void;
  onEdit: (memory: Memory) => void;
  onDelete: (memory: Memory) => void;
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  // Bulk delete props (Issue #666). ``selectedIds`` holds ``Memory.id`` UUIDs
  // for rows already normalized into the ``Memory`` shape this table renders
  // (which expects ``key`` / ``agent_name`` columns — so a consumer fed from
  // raw ``GET /api/v1/memory/list`` rows must convert at the boundary first).
  //
  // Bulk-delete wiring is the parent's responsibility. The legacy
  // ``bulkDeleteMemories`` API addresses rows by (key, scope, agent_name),
  // which ``/api/v1/memory/list`` does not return; a consumer fed from that
  // endpoint needs either a bulk-delete-by-id backend endpoint or pre-loaded
  // composite fields. #433 will decide which path to take.
  selectedIds?: string[];
  onSelectionChange?: (ids: string[]) => void;
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
  selectedIds = [],
  onSelectionChange,
}: MemoriesTableProps) {
  const { user } = useAuth();
  const locale = useLocale();
  const totalPages = Math.ceil(total / pageSize);
  const bulkDeleteEnabled = !!onSelectionChange;

  const allVisibleSelected =
    memories.length > 0 && memories.every((m) => selectedIds.includes(m.id));

  // Radix Checkbox `onCheckedChange` emits `boolean | "indeterminate"`.
  // Coerce explicitly so the "indeterminate" branch is treated as unchecked,
  // not as a silent truthy "select all".
  const handleSelectAll = (checked: boolean | "indeterminate") => {
    if (!onSelectionChange) return;

    if (checked === true) {
      const visibleIds = memories.map((m) => m.id);
      onSelectionChange([...new Set([...selectedIds, ...visibleIds])]);
    } else {
      const visibleIds = new Set(memories.map((m) => m.id));
      onSelectionChange(selectedIds.filter((id) => !visibleIds.has(id)));
    }
  };

  const handleRowToggle = (
    memory: Memory,
    checked: boolean | "indeterminate",
  ) => {
    if (!onSelectionChange) return;

    if (checked === true) {
      onSelectionChange([...selectedIds, memory.id]);
    } else {
      onSelectionChange(selectedIds.filter((id) => id !== memory.id));
    }
  };

  const getImportanceBadge = (importance: number) => {
    if (importance >= 0.8) return <Badge variant="destructive">High</Badge>;
    if (importance >= 0.5) return <Badge variant="default">Medium</Badge>;
    return <Badge variant="secondary">Low</Badge>;
  };

  const getScopeBadge = (scope: string) => {
    return scope === "persistent" ? (
      <Badge variant="default">Persistent</Badge>
    ) : (
      <Badge variant="outline">Working</Badge>
    );
  };

  if (loading && memories.length === 0) {
    return (
      <div className="flex items-center justify-center h-64">
        <SpinnerLoading size="md" message="Loading memories..." />
      </div>
    );
  }

  if (!loading && memories.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 border border-dashed rounded-lg">
        <div className="text-center">
          <p className="text-lg font-medium text-slate-900 dark:text-white">
            No memories found
          </p>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Try adjusting your search or filters
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-slate-200 dark:border-slate-800">
        <Table>
          <TableHeader>
            <TableRow>
              {bulkDeleteEnabled && (
                <TableHead className="w-12">
                  <Checkbox
                    checked={allVisibleSelected}
                    onCheckedChange={handleSelectAll}
                    aria-label="Select all"
                  />
                </TableHead>
              )}
              <TableHead>Key</TableHead>
              <TableHead>Scope</TableHead>
              <TableHead>Agent</TableHead>
              <TableHead>Importance</TableHead>
              <TableHead>Updated</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {memories.map((memory) => {
              const isSelected = selectedIds.includes(memory.id);
              return (
                <TableRow key={memory.id}>
                  {bulkDeleteEnabled && (
                    <TableCell>
                      <Checkbox
                        checked={isSelected}
                        onCheckedChange={(checked) =>
                          handleRowToggle(memory, checked)
                        }
                        aria-label={`Select ${memory.key}`}
                      />
                    </TableCell>
                  )}
                  <TableCell className="font-medium max-w-xs truncate">
                    {memory.key}
                  </TableCell>
                  <TableCell>{getScopeBadge(memory.scope)}</TableCell>
                  <TableCell>
                    <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded">
                      {memory.agent_name}
                    </code>
                  </TableCell>
                  <TableCell>{getImportanceBadge(memory.importance)}</TableCell>
                  <TableCell className="text-sm text-slate-500">
                    {formatRelativeTime(
                      memory.updated_at,
                      user?.timezone,
                      locale,
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex items-center justify-end gap-2">
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => onView(memory)}
                        title="View details"
                      >
                        <Eye className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => onEdit(memory)}
                        title="Edit memory"
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => onDelete(memory)}
                        title="Delete memory"
                        className="text-red-600 hover:text-red-700 hover:bg-red-50 dark:hover:bg-red-900/20"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-slate-500">
            Showing {(page - 1) * pageSize + 1} to{" "}
            {Math.min(page * pageSize, total)} of {total} memories
          </p>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onPageChange(page - 1)}
              disabled={page === 1 || loading}
            >
              Previous
            </Button>
            <span className="text-sm text-slate-700 dark:text-slate-300">
              Page {page} of {totalPages}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => onPageChange(page + 1)}
              disabled={page === totalPages || loading}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
