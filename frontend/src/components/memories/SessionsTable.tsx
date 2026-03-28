/**
 * Sessions Table Component
 *
 * Displays coding sessions in a table with pagination
 * Issue #666: Phase 2 - Memory List Enhancement
 */

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Eye, ExternalLink } from 'lucide-react';
import type { SessionSummary } from '@/lib/api/coding-sessions';
import { formatDuration, formatSessionDate } from '@/lib/api/coding-sessions';
import { SpinnerLoading } from '@/components/common/LoadingState';

interface SessionsTableProps {
  sessions: SessionSummary[];
  loading: boolean;
  onViewDetail: (session: SessionSummary) => void;
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
}

export function SessionsTable({
  sessions,
  loading,
  onViewDetail,
  page,
  pageSize,
  total,
  onPageChange,
}: SessionsTableProps) {
  const totalPages = Math.ceil(total / pageSize);

  const getStatusBadge = (success: boolean | undefined) => {
    if (success === undefined) {
      return <Badge variant="outline">In Progress</Badge>;
    }
    return success ? (
      <Badge variant="default" className="bg-green-600">Success</Badge>
    ) : (
      <Badge variant="destructive">Failed</Badge>
    );
  };

  if (loading && sessions.length === 0) {
    return (
      <div className="flex items-center justify-center h-64">
        <SpinnerLoading size="md" message="Loading sessions..." />
      </div>
    );
  }

  if (!loading && sessions.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 border border-dashed rounded-lg">
        <div className="text-center">
          <p className="text-lg font-medium text-slate-900 dark:text-white">No sessions found</p>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Start a coding session to see it here
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
              <TableHead>Context</TableHead>
              <TableHead>Description</TableHead>
              <TableHead>Date</TableHead>
              <TableHead>Duration</TableHead>
              <TableHead>Statistics</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sessions.map((session) => (
              <TableRow key={session.id}>
                <TableCell className="font-medium">
                  <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded">
                    {session.context_id}
                  </code>
                </TableCell>
                <TableCell className="max-w-xs truncate">
                  {session.description}
                </TableCell>
                <TableCell className="text-sm text-slate-500">
                  {formatSessionDate(session.start_time)}
                </TableCell>
                <TableCell className="text-sm">
                  {formatDuration(session.duration_seconds)}
                </TableCell>
                <TableCell>
                  <div className="flex flex-wrap gap-1 text-xs">
                    <Badge variant="outline" className="font-mono">
                      {session.file_changes_count} files
                    </Badge>
                    <Badge variant="outline" className="font-mono">
                      {session.decisions_count} decisions
                    </Badge>
                    <Badge variant="outline" className="font-mono">
                      {session.errors_count} errors
                    </Badge>
                  </div>
                </TableCell>
                <TableCell>
                  {getStatusBadge(session.success)}
                </TableCell>
                <TableCell className="text-right">
                  <div className="flex items-center justify-end gap-2">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => onViewDetail(session)}
                      title="View session details"
                    >
                      <Eye className="h-4 w-4" />
                    </Button>
                    {session.github_issue && (
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => window.open(`https://github.com/JFK/kagura-ai/issues/${session.github_issue}`, '_blank')}
                        title={`View GitHub Issue #${session.github_issue}`}
                      >
                        <ExternalLink className="h-4 w-4" />
                      </Button>
                    )}
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
            Showing {(page - 1) * pageSize + 1} to {Math.min(page * pageSize, total)} of {total} sessions
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
