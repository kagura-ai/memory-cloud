/**
 * Memory Detail Dialog
 *
 * Displays detailed information about a memory
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
import { Pencil, Trash2, Copy, Check } from 'lucide-react';
import type { Memory } from '@/lib/types/memory';
import { format } from 'date-fns';
import { useState } from 'react';

interface MemoryDetailDialogProps {
  memory: Memory;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onEdit: () => void;
  onDelete: () => void;
}

export function MemoryDetailDialog({
  memory,
  open,
  onOpenChange,
  onEdit,
  onDelete,
}: MemoryDetailDialogProps) {
  const [copied, setCopied] = useState(false);

  const copyValue = async () => {
    try {
      await navigator.clipboard.writeText(memory.value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (error) {
      console.error('Failed to copy:', error);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {memory.key}
            <Badge variant={memory.scope === 'persistent' ? 'default' : 'outline'}>
              {memory.scope}
            </Badge>
          </DialogTitle>
          <DialogDescription>
            Memory details and metadata
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Value */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm font-medium">Value</label>
              <Button
                variant="ghost"
                size="sm"
                onClick={copyValue}
                className="h-8"
              >
                {copied ? (
                  <>
                    <Check className="h-4 w-4 mr-1" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="h-4 w-4 mr-1" />
                    Copy
                  </>
                )}
              </Button>
            </div>
            <div className="p-3 bg-slate-50 dark:bg-slate-900 rounded-lg text-sm font-mono whitespace-pre-wrap break-words">
              {memory.value}
            </div>
          </div>

          <Separator />

          {/* Metadata Grid */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                Agent Name
              </label>
              <p className="mt-1">
                <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded">
                  {memory.agent_name}
                </code>
              </p>
            </div>

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                User ID
              </label>
              <p className="mt-1 text-sm truncate">{memory.user_id}</p>
            </div>

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                Importance
              </label>
              <p className="mt-1">
                <Badge variant={
                  memory.importance >= 0.8 ? 'destructive' :
                  memory.importance >= 0.5 ? 'default' : 'secondary'
                }>
                  {memory.importance.toFixed(2)}
                </Badge>
              </p>
            </div>

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                Access Count
              </label>
              <p className="mt-1 text-sm">{memory.access_count || 0}</p>
            </div>

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                Created At
              </label>
              <p className="mt-1 text-sm">
                {format(new Date(memory.created_at), 'PPpp')}
              </p>
            </div>

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                Updated At
              </label>
              <p className="mt-1 text-sm">
                {format(new Date(memory.updated_at), 'PPpp')}
              </p>
            </div>
          </div>

          {/* Tags */}
          {memory.tags && memory.tags.length > 0 && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  Tags
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
          {memory.metadata && Object.keys(memory.metadata).length > 0 && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  Metadata
                </label>
                <div className="mt-2 p-3 bg-slate-50 dark:bg-slate-900 rounded-lg text-sm font-mono">
                  <pre>{JSON.stringify(memory.metadata, null, 2)}</pre>
                </div>
              </div>
            </>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          <Button variant="outline" onClick={onEdit}>
            <Pencil className="h-4 w-4 mr-2" />
            Edit
          </Button>
          <Button variant="destructive" onClick={onDelete}>
            <Trash2 className="h-4 w-4 mr-2" />
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
