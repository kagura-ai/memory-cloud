'use client';

/**
 * Edit Memory Dialog
 *
 * Form for editing an existing memory
 */

import { useState, useEffect } from 'react';
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
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { updateMemory } from '@/lib/api/memory';
import type { Memory } from '@/lib/types/memory';

interface EditMemoryDialogProps {
  memory: Memory;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
}

export function EditMemoryDialog({
  memory,
  open,
  onOpenChange,
  onSuccess,
}: EditMemoryDialogProps) {
  const { user } = useAuth();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form state
  const [value, setValue] = useState(memory.value);
  const [importance, setImportance] = useState(memory.importance.toString());
  const [tags, setTags] = useState(memory.tags?.join(', ') || '');

  // Reset form when memory changes
  useEffect(() => {
    setValue(memory.value);
    setImportance(memory.importance.toString());
    setTags(memory.tags?.join(', ') || '');
  }, [memory]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!user) {
      setError('User not authenticated');
      return;
    }

    if (!value.trim()) {
      setError('Value is required');
      return;
    }

    try {
      setLoading(true);
      setError(null);

      await updateMemory(
        memory.key,
        user.id,
        {
          value: value.trim(),
          importance: parseFloat(importance),
          tags: tags.trim() ? tags.split(',').map(t => t.trim()).filter(Boolean) : undefined,
        },
        memory.scope,
        memory.agent_name
      );

      onSuccess();
    } catch (err) {
      console.error('Failed to update memory:', err);
      setError(err instanceof Error ? err.message : 'Failed to update memory');
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
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Edit Memory</DialogTitle>
          <DialogDescription>
            Update memory: <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded">{memory.key}</code>
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="p-3 text-sm text-red-600 bg-red-50 dark:bg-red-900/20 rounded-md">
              {error}
            </div>
          )}

          {/* Read-only fields */}
          <div className="grid grid-cols-2 gap-4 p-3 bg-slate-50 dark:bg-slate-900 rounded-lg">
            <div>
              <Label className="text-xs text-slate-500">Scope</Label>
              <p className="text-sm font-medium">{memory.scope}</p>
            </div>
            <div>
              <Label className="text-xs text-slate-500">Agent</Label>
              <p className="text-sm font-medium">{memory.agent_name}</p>
            </div>
          </div>

          {/* Value */}
          <div className="space-y-2">
            <Label htmlFor="value">
              Value <span className="text-red-500">*</span>
            </Label>
            <Textarea
              id="value"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="Enter memory value..."
              rows={6}
              required
            />
          </div>

          {/* Importance */}
          <div className="space-y-2">
            <Label htmlFor="importance">
              Importance (0.0 - 1.0)
            </Label>
            <Input
              id="importance"
              type="number"
              min="0"
              max="1"
              step="0.1"
              value={importance}
              onChange={(e) => setImportance(e.target.value)}
            />
          </div>

          {/* Tags */}
          <div className="space-y-2">
            <Label htmlFor="tags">Tags (comma-separated)</Label>
            <Input
              id="tags"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="tag1, tag2, tag3"
            />
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={handleClose}
              disabled={loading}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={loading}>
              {loading ? 'Updating...' : 'Update Memory'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
