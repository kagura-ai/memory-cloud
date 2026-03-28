import { type SessionSummary } from '@/lib/api/coding-sessions';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Eye,
  Calendar,
  Clock,
  FileCode,
  Lightbulb,
  AlertCircle,
  CheckCircle,
  XCircle,
  Github,
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';

interface SessionCardProps {
  session: SessionSummary;
  onView?: (sessionId: string) => void;
}

function formatDuration(seconds?: number): string {
  if (!seconds) return 'In progress';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

export function SessionCard({ session, onView }: SessionCardProps) {
  const isComplete = !!session.end_time;
  const isSuccess = session.success !== false;

  const statusGradient = isSuccess
    ? 'from-green-500 to-emerald-500'
    : 'from-orange-500 to-red-500';

  return (
    <Card className="group relative overflow-hidden border-gray-200 bg-white transition-all hover:border-brand-green-300 hover:shadow-xl hover:-translate-y-1">
      {/* Gradient overlay on hover */}
      <div className={`pointer-events-none absolute inset-0 bg-gradient-to-br ${statusGradient} opacity-0 transition-opacity group-hover:opacity-5`} />

      <CardContent className="relative p-6">
        {/* Header */}
        <div className="mb-4 flex items-start justify-between">
          <div className="flex-1">
            <div className="mb-2 flex items-center gap-2">
              <div className={`inline-flex rounded-lg bg-gradient-to-br ${statusGradient} p-2 text-white shadow-lg`}>
                {isSuccess ? (
                  <CheckCircle className="h-4 w-4" />
                ) : (
                  <XCircle className="h-4 w-4" />
                )}
              </div>
              <Badge
                variant={isComplete ? 'default' : 'secondary'}
                className={isComplete ? 'bg-brand-green-600' : 'bg-gray-400'}
              >
                {isComplete ? 'Completed' : 'In Progress'}
              </Badge>
            </div>

            <h3 className="mb-1 line-clamp-2 text-lg font-semibold text-gray-900">
              {session.context_id}
            </h3>

            <p className="mb-3 line-clamp-2 text-sm text-gray-600">
              {session.description || 'No description'}
            </p>
          </div>
        </div>

        {/* Stats Grid */}
        <div className="mb-4 grid grid-cols-3 gap-3">
          <div className="rounded-lg bg-blue-50 p-3 text-center">
            <div className="mb-1 flex justify-center">
              <FileCode className="h-4 w-4 text-blue-600" />
            </div>
            <div className="text-lg font-bold text-gray-900">
              {session.file_changes_count}
            </div>
            <div className="text-xs text-gray-600">Files</div>
          </div>

          <div className="rounded-lg bg-purple-50 p-3 text-center">
            <div className="mb-1 flex justify-center">
              <Lightbulb className="h-4 w-4 text-purple-600" />
            </div>
            <div className="text-lg font-bold text-gray-900">
              {session.decisions_count}
            </div>
            <div className="text-xs text-gray-600">Decisions</div>
          </div>

          <div className="rounded-lg bg-orange-50 p-3 text-center">
            <div className="mb-1 flex justify-center">
              <AlertCircle className="h-4 w-4 text-orange-600" />
            </div>
            <div className="text-lg font-bold text-gray-900">
              {session.errors_count}
            </div>
            <div className="text-xs text-gray-600">Errors</div>
          </div>
        </div>

        {/* Metadata */}
        <div className="mb-4 space-y-2 text-xs text-gray-600">
          <div className="flex items-center gap-2">
            <Calendar className="h-3.5 w-3.5" />
            <span>
              Started {formatDistanceToNow(new Date(session.start_time), { addSuffix: true })}
            </span>
          </div>

          {session.duration_seconds !== undefined && (
            <div className="flex items-center gap-2">
              <Clock className="h-3.5 w-3.5" />
              <span>Duration: {formatDuration(session.duration_seconds)}</span>
            </div>
          )}

          {session.github_issue && (
            <div className="flex items-center gap-2">
              <Github className="h-3.5 w-3.5" />
              <a
                href={`https://github.com/JFK/kagura-ai/issues/${session.github_issue}`}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-brand-green-600 hover:underline"
              >
                Issue #{session.github_issue}
              </a>
            </div>
          )}
        </div>

        {/* Actions */}
        <Button
          variant="outline"
          size="sm"
          onClick={() => onView?.(session.id)}
          className="w-full border-gray-300 text-sm"
        >
          <Eye className="mr-2 h-4 w-4" />
          View Details
        </Button>
      </CardContent>
    </Card>
  );
}
