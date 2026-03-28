/**
 * Session Detail Dialog
 *
 * Displays detailed information about a coding session with tabbed content
 * Issue #666: Phase 2 - Memory List Enhancement
 */

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ExternalLink, Clock, Calendar, CheckCircle, XCircle } from 'lucide-react';
import type {
  SessionDetailResponse,
  FileChange,
  Decision,
  ErrorRecord,
} from '@/lib/api/coding-sessions';
import { formatDuration } from '@/lib/api/coding-sessions';
import { format } from 'date-fns';
import { useState, useEffect } from 'react';
import { getCodingSessionDetail } from '@/lib/api/coding-sessions';
import { SpinnerLoading } from '@/components/common/LoadingState';

interface SessionDetailDialogProps {
  sessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function SessionDetailDialog({
  sessionId,
  open,
  onOpenChange,
}: SessionDetailDialogProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionDetail, setSessionDetail] = useState<SessionDetailResponse | null>(null);

  useEffect(() => {
    if (open && sessionId) {
      fetchSessionDetail();
    }
  }, [open, sessionId]);

  const fetchSessionDetail = async () => {
    setLoading(true);
    setError(null);
    try {
      const detail = await getCodingSessionDetail(sessionId);
      setSessionDetail(detail);
    } catch (err) {
      console.error('Failed to fetch session detail:', err);
      setError(err instanceof Error ? err.message : 'Failed to load session details');
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-4xl max-h-[80vh]">
          <div className="flex items-center justify-center h-64">
            <SpinnerLoading size="md" message="Loading session details..." />
          </div>
        </DialogContent>
      </Dialog>
    );
  }

  if (error || !sessionDetail) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-4xl">
          <div className="p-4 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 rounded-lg">
            {error || 'Failed to load session details'}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  const { session, file_changes, decisions, errors } = sessionDetail;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 flex-wrap">
            <span className="truncate">{session.description}</span>
            <Badge variant="outline">{session.context_id}</Badge>
            {session.success !== undefined && (
              session.success ? (
                <Badge variant="default" className="bg-green-600 flex items-center gap-1">
                  <CheckCircle className="h-3 w-3" />
                  Success
                </Badge>
              ) : (
                <Badge variant="destructive" className="flex items-center gap-1">
                  <XCircle className="h-3 w-3" />
                  Failed
                </Badge>
              )
            )}
          </DialogTitle>
          <DialogDescription>
            Session details and activity log
          </DialogDescription>
        </DialogHeader>

        {/* Session Metadata */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 p-4 bg-slate-50 dark:bg-slate-900 rounded-lg">
          <div>
            <div className="flex items-center gap-1 text-sm text-slate-500 dark:text-slate-400 mb-1">
              <Calendar className="h-3 w-3" />
              Start Time
            </div>
            <p className="text-sm font-medium">
              {format(new Date(session.start_time), 'PPpp')}
            </p>
          </div>
          <div>
            <div className="flex items-center gap-1 text-sm text-slate-500 dark:text-slate-400 mb-1">
              <Clock className="h-3 w-3" />
              Duration
            </div>
            <p className="text-sm font-medium">
              {formatDuration(session.duration_seconds)}
            </p>
          </div>
          <div>
            <div className="text-sm text-slate-500 dark:text-slate-400 mb-1">
              File Changes
            </div>
            <p className="text-sm font-medium">{session.file_changes_count}</p>
          </div>
          <div>
            <div className="text-sm text-slate-500 dark:text-slate-400 mb-1">
              Decisions / Errors
            </div>
            <p className="text-sm font-medium">
              {session.decisions_count} / {session.errors_count}
            </p>
          </div>
        </div>

        {/* GitHub Issue Link */}
        {session.github_issue && (
          <div className="flex items-center gap-2 p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg">
            <ExternalLink className="h-4 w-4 text-blue-600 dark:text-blue-400" />
            <span className="text-sm text-blue-600 dark:text-blue-400">
              Linked to GitHub Issue
            </span>
            <Button
              variant="link"
              size="sm"
              className="p-0 h-auto text-blue-600 dark:text-blue-400"
              onClick={() =>
                window.open(
                  `https://github.com/JFK/kagura-ai/issues/${session.github_issue}`,
                  '_blank'
                )
              }
            >
              #{session.github_issue}
            </Button>
          </div>
        )}

        <Separator />

        {/* Tabbed Content */}
        <Tabs defaultValue="files" className="w-full">
          <TabsList className="grid w-full grid-cols-3">
            <TabsTrigger value="files">
              File Changes ({file_changes.length})
            </TabsTrigger>
            <TabsTrigger value="decisions">
              Decisions ({decisions.length})
            </TabsTrigger>
            <TabsTrigger value="errors">Errors ({errors.length})</TabsTrigger>
          </TabsList>

          {/* File Changes Tab */}
          <TabsContent value="files" className="mt-4 space-y-3">
            {file_changes.length === 0 ? (
              <div className="text-center py-8 text-slate-500">
                No file changes recorded
              </div>
            ) : (
              file_changes.map((change, index) => (
                <FileChangeCard key={index} change={change} />
              ))
            )}
          </TabsContent>

          {/* Decisions Tab */}
          <TabsContent value="decisions" className="mt-4 space-y-3">
            {decisions.length === 0 ? (
              <div className="text-center py-8 text-slate-500">
                No decisions recorded
              </div>
            ) : (
              decisions.map((decision, index) => (
                <DecisionCard key={index} decision={decision} />
              ))
            )}
          </TabsContent>

          {/* Errors Tab */}
          <TabsContent value="errors" className="mt-4 space-y-3">
            {errors.length === 0 ? (
              <div className="text-center py-8 text-slate-500">
                No errors recorded
              </div>
            ) : (
              errors.map((error, index) => (
                <ErrorCard key={index} error={error} />
              ))
            )}
          </TabsContent>
        </Tabs>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// File Change Card Component
function FileChangeCard({ change }: { change: FileChange }) {
  return (
    <div className="p-4 border border-slate-200 dark:border-slate-800 rounded-lg space-y-2">
      <div className="flex items-start justify-between gap-2">
        <code className="text-sm font-mono bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded flex-1 break-all">
          {change.file_path}
        </code>
        <Badge variant={
          change.action === 'create' ? 'default' :
          change.action === 'delete' ? 'destructive' :
          'outline'
        }>
          {change.action}
        </Badge>
      </div>
      {change.reason && (
        <p className="text-sm text-slate-600 dark:text-slate-400">
          <span className="font-medium">Reason: </span>
          {change.reason}
        </p>
      )}
      {change.line_range && (
        <p className="text-xs text-slate-500">
          Lines: {change.line_range}
        </p>
      )}
      {change.diff && (
        <div className="mt-2">
          <details className="text-xs">
            <summary className="cursor-pointer text-slate-500 hover:text-slate-700 dark:hover:text-slate-300">
              View diff
            </summary>
            <pre className="mt-2 p-2 bg-slate-900 text-slate-100 rounded overflow-x-auto">
              {change.diff}
            </pre>
          </details>
        </div>
      )}
    </div>
  );
}

// Decision Card Component
function DecisionCard({ decision }: { decision: Decision }) {
  return (
    <div className="p-4 border border-slate-200 dark:border-slate-800 rounded-lg space-y-3">
      <div>
        <h4 className="font-medium text-sm mb-1">Decision</h4>
        <p className="text-sm text-slate-700 dark:text-slate-300">{decision.decision}</p>
      </div>
      <div>
        <h4 className="font-medium text-sm mb-1">Rationale</h4>
        <p className="text-sm text-slate-600 dark:text-slate-400">{decision.rationale}</p>
      </div>
      {decision.alternatives && decision.alternatives.length > 0 && (
        <div>
          <h4 className="font-medium text-sm mb-1">Alternatives Considered</h4>
          <ul className="list-disc list-inside text-sm text-slate-600 dark:text-slate-400 space-y-1">
            {decision.alternatives.map((alt, idx) => (
              <li key={idx}>{alt}</li>
            ))}
          </ul>
        </div>
      )}
      {decision.impact && (
        <div>
          <h4 className="font-medium text-sm mb-1">Impact</h4>
          <p className="text-sm text-slate-600 dark:text-slate-400">{decision.impact}</p>
        </div>
      )}
      {decision.tags && decision.tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {decision.tags.map((tag) => (
            <Badge key={tag} variant="outline" className="text-xs">
              {tag}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}

// Error Card Component
function ErrorCard({ error }: { error: ErrorRecord }) {
  return (
    <div className="p-4 border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-900/20 rounded-lg space-y-2">
      <div className="flex items-start justify-between gap-2">
        <h4 className="font-medium text-sm text-red-900 dark:text-red-300">
          {error.error_type}
        </h4>
        {error.solution && (
          <Badge variant="default" className="bg-green-600">
            Solved
          </Badge>
        )}
      </div>
      <p className="text-sm text-red-800 dark:text-red-200">{error.message}</p>
      {error.file_path && (
        <p className="text-xs text-red-700 dark:text-red-300 font-mono">
          {error.file_path}
          {error.line_number && `:${error.line_number}`}
        </p>
      )}
      {error.solution && (
        <div className="mt-2 pt-2 border-t border-red-300 dark:border-red-800">
          <h5 className="text-xs font-medium text-green-900 dark:text-green-300 mb-1">
            Solution:
          </h5>
          <p className="text-sm text-green-800 dark:text-green-200">
            {error.solution}
          </p>
        </div>
      )}
    </div>
  );
}
