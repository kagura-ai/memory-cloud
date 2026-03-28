import { type Memory } from '@/lib/types/memory';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Eye,
  Edit,
  Trash2,
  Calendar,
  Tag,
  Activity,
  Database,
  HardDrive,
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';

interface MemoryCardProps {
  memory: Memory;
  onView?: (memory: Memory) => void;
  onEdit?: (memory: Memory) => void;
  onDelete?: (memory: Memory) => void;
}

export function MemoryCard({ memory, onView, onEdit, onDelete }: MemoryCardProps) {
  const scopeGradient =
    memory.scope === 'working'
      ? 'from-blue-500 to-cyan-500'
      : 'from-purple-500 to-pink-500';

  const scopeColor = memory.scope === 'working' ? 'bg-blue-100 text-blue-700' : 'bg-purple-100 text-purple-700';

  return (
    <Card className="group relative overflow-hidden border-gray-200 bg-white transition-all hover:border-brand-green-300 hover:shadow-xl hover:-translate-y-1">
      {/* Gradient overlay on hover */}
      <div className={`pointer-events-none absolute inset-0 bg-gradient-to-br ${scopeGradient} opacity-0 transition-opacity group-hover:opacity-5`} />

      <CardContent className="relative p-6">
        {/* Header */}
        <div className="mb-4 flex items-start justify-between">
          <div className="flex-1">
            <div className="mb-2 flex items-center gap-2">
              <div className={`inline-flex rounded-lg p-2 ${scopeColor}`}>
                {memory.scope === 'working' ? (
                  <Activity className="h-4 w-4" />
                ) : (
                  <Database className="h-4 w-4" />
                )}
              </div>
              <Badge variant="outline" className="border-gray-300 text-xs">
                {memory.scope}
              </Badge>
            </div>

            <h3 className="mb-1 line-clamp-2 text-lg font-semibold text-gray-900">
              {memory.key}
            </h3>

            <p className="mb-3 line-clamp-3 text-sm text-gray-600">{memory.value}</p>
          </div>
        </div>

        {/* Metadata */}
        <div className="mb-4 space-y-2 text-xs text-gray-600">
          <div className="flex items-center gap-2">
            <Activity className="h-3.5 w-3.5" />
            <span className="font-medium">{memory.agent_name}</span>
          </div>

          <div className="flex items-center gap-2">
            <Calendar className="h-3.5 w-3.5" />
            <span>{formatDistanceToNow(new Date(memory.created_at), { addSuffix: true })}</span>
          </div>

          {memory.access_count !== undefined && (
            <div className="flex items-center gap-2">
              <Eye className="h-3.5 w-3.5" />
              <span>{memory.access_count} accesses</span>
            </div>
          )}
        </div>

        {/* Tags */}
        {memory.tags && memory.tags.length > 0 && (
          <div className="mb-4 flex flex-wrap gap-1.5">
            {memory.tags.slice(0, 3).map((tag) => (
              <Badge
                key={tag}
                variant="secondary"
                className="bg-gray-100 text-xs font-normal text-gray-700 hover:bg-gray-200"
              >
                <Tag className="mr-1 h-3 w-3" />
                {tag}
              </Badge>
            ))}
            {memory.tags.length > 3 && (
              <Badge variant="secondary" className="bg-gray-100 text-xs text-gray-600">
                +{memory.tags.length - 3}
              </Badge>
            )}
          </div>
        )}

        {/* Importance Bar */}
        <div className="mb-4">
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="text-gray-600">Importance</span>
            <span className="font-medium text-gray-900">{(memory.importance * 10).toFixed(1)}/10</span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-gray-200">
            <div
              className={`h-full rounded-full bg-gradient-to-r ${scopeGradient} transition-all`}
              style={{ width: `${memory.importance * 100}%` }}
            />
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => onView?.(memory)}
            className="flex-1 border-gray-300 text-xs"
          >
            <Eye className="mr-1.5 h-3.5 w-3.5" />
            View
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => onEdit?.(memory)}
            className="flex-1 border-gray-300 text-xs"
          >
            <Edit className="mr-1.5 h-3.5 w-3.5" />
            Edit
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => onDelete?.(memory)}
            className="border-red-200 text-xs text-red-600 hover:bg-red-50"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
