'use client';

/**
 * Create Memory Dialog
 *
 * Form for creating a new memory
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
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { createMemory } from '@/lib/api/memory';
import type { MemoryScope } from '@/lib/types/memory';

interface CreateMemoryDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
}

export function CreateMemoryDialog({
  open,
  onOpenChange,
  onSuccess,
}: CreateMemoryDialogProps) {
  const { user } = useAuth();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form state
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [scope, setScope] = useState<MemoryScope>('persistent');
  const [agentName, setAgentName] = useState('global');
  const [importance, setImportance] = useState('0.5');
  const [tags, setTags] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!user) {
      setError('User not authenticated');
      return;
    }

    if (!key.trim() || !value.trim()) {
      setError('Key and value are required');
      return;
    }

    try {
      setLoading(true);
      setError(null);

      await createMemory(user.id, {
        key: key.trim(),
        value: value.trim(),
        scope,
        agent_name: agentName,
        importance: parseFloat(importance),
        tags: tags.trim() ? tags.split(',').map(t => t.trim()).filter(Boolean) : undefined,
      });

      // Reset form
      setKey('');
      setValue('');
      setScope('persistent');
      setAgentName('global');
      setImportance('0.5');
      setTags('');

      onSuccess();
    } catch (err) {
      console.error('Failed to create memory:', err);
      setError(err instanceof Error ? err.message : 'Failed to create memory');
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    if (!loading) {
      onOpenChange(false);
      // Reset error when closing
      setError(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Create New Memory</DialogTitle>
          <DialogDescription>
            Add a new memory to your memory cloud
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="p-3 text-sm text-red-600 bg-red-50 dark:bg-red-900/20 rounded-md">
              {error}
            </div>
          )}

          {/* Key */}
          <div className="space-y-2">
            <Label htmlFor="key">
              Key <span className="text-red-500">*</span>
            </Label>
            <Input
              id="key"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="e.g., user_preference_theme"
              required
            />
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
              rows={4}
              required
            />
          </div>

          {/* Grid layout for smaller fields */}
          <div className="grid grid-cols-2 gap-4">
            {/* Scope */}
            <div className="space-y-2">
              <Label htmlFor="scope">Scope</Label>
              <Select value={scope} onValueChange={(value) => setScope(value as MemoryScope)}>
                <SelectTrigger id="scope">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="persistent">Persistent (Disk)</SelectItem>
                  <SelectItem value="working">Working (RAM)</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {/* Agent Name */}
            <div className="space-y-2">
              <Label htmlFor="agent">Agent Name</Label>
              <Input
                id="agent"
                value={agentName}
                onChange={(e) => setAgentName(e.target.value)}
                placeholder="global"
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
              {loading ? 'Creating...' : 'Create Memory'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
