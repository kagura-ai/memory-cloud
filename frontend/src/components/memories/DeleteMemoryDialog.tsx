'use client';

/**
 * Delete Memory Dialog
 *
 * Confirmation dialog for deleting a memory
 */

import { useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { AlertTriangle } from 'lucide-react';
import { deleteMemory } from '@/lib/api/memory';
import type { Memory } from '@/lib/types/memory';

interface DeleteMemoryDialogProps {
  memory: Memory;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
}

export function DeleteMemoryDialog({
  memory,
  open,
  onOpenChange,
  onSuccess,
}: DeleteMemoryDialogProps) {
  const { user } = useAuth();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDelete = async () => {
    if (!user) {
      setError('User not authenticated');
      return;
    }

    try {
      setLoading(true);
      setError(null);

      await deleteMemory(
        memory.key,
        user.id,
        memory.scope,
        memory.agent_name
      );

      onSuccess();
    } catch (err) {
      console.error('Failed to delete memory:', err);
      setError(err instanceof Error ? err.message : 'Failed to delete memory');
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    if (!loading) {
      onOpenChange(false);
      setError(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-red-600 dark:text-red-400">
            <AlertTriangle className="h-5 w-5" />
            Delete Memory
          </DialogTitle>
          <DialogDescription>
            Are you sure you want to delete this memory? This action cannot be undone.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {error && (
            <div className="p-3 text-sm text-red-600 bg-red-50 dark:bg-red-900/20 rounded-md">
              {error}
            </div>
          )}

          <div className="p-4 bg-slate-50 dark:bg-slate-900 rounded-lg space-y-2">
            <div>
              <span className="text-xs text-slate-500 dark:text-slate-400">Key:</span>
              <p className="font-medium">{memory.key}</p>
            </div>
            <div>
              <span className="text-xs text-slate-500 dark:text-slate-400">Scope:</span>
              <p className="text-sm">{memory.scope}</p>
            </div>
            <div>
              <span className="text-xs text-slate-500 dark:text-slate-400">Agent:</span>
              <p className="text-sm">{memory.agent_name}</p>
            </div>
            <div>
              <span className="text-xs text-slate-500 dark:text-slate-400">Value (preview):</span>
              <p className="text-sm text-slate-600 dark:text-slate-300 truncate">
                {memory.value.substring(0, 100)}
                {memory.value.length > 100 && '...'}
              </p>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={handleClose}
            disabled={loading}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={handleDelete}
            disabled={loading}
          >
            {loading ? 'Deleting...' : 'Delete Memory'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
