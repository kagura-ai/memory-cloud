/**
 * Delete API Key Dialog
 *
 * Issue #655 - Confirmation dialog for deleting API keys
 */

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { AlertCircle } from 'lucide-react';
import { deleteSystemAPIKey } from '@/lib/api/api-keys';

interface DeleteAPIKeyDialogProps {
  isOpen: boolean;
  keyId: number;
  keyName: string;
  onClose: () => void;
  onSuccess: () => void;
}

export function DeleteAPIKeyDialog({
  isOpen,
  keyId,
  keyName,
  onClose,
  onSuccess,
}: DeleteAPIKeyDialogProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDelete = async () => {
    try {
      setLoading(true);
      setError(null);

      // Hard delete - permanently removes key from database
      await deleteSystemAPIKey(keyId);

      // Success - close dialog and refresh list
      // Don't set loading to false here since component will unmount
      onSuccess();
    } catch (err) {
      console.error('Failed to delete API key:', err);
      const errorMessage = (err as { message?: string })?.message || 'Failed to delete API key';
      setError(errorMessage);
      setLoading(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>Delete API Key</DialogTitle>
          <DialogDescription>
            Permanently remove this API key from the database
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>
              <strong>Warning:</strong> This action cannot be undone. The API key will be permanently deleted from the database, including all audit history.
            </AlertDescription>
          </Alert>

          {error && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <div className="space-y-2">
            <p className="text-sm">
              Are you sure you want to permanently delete the API key <strong>"{keyName}"</strong>?
            </p>
            <p className="text-xs text-slate-500">
              Consider revoking instead if you want to keep audit history.
            </p>
          </div>
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={loading}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={handleDelete}
            disabled={loading}
          >
            {loading ? 'Deleting...' : 'Delete Permanently'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
