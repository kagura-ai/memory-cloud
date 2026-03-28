/**
 * Revoke API Key Dialog
 *
 * Issue #655 - Confirmation dialog for revoking API keys
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
import { revokeAPIKey } from '@/lib/api/api-keys';

interface RevokeAPIKeyDialogProps {
  isOpen: boolean;
  keyId: number;
  keyName: string;
  onClose: () => void;
  onSuccess: () => void;
}

export function RevokeAPIKeyDialog({
  isOpen,
  keyId,
  keyName,
  onClose,
  onSuccess,
}: RevokeAPIKeyDialogProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleRevoke = async () => {
    try {
      setLoading(true);
      setError(null);

      await revokeAPIKey(keyId);

      // Close dialog immediately on success (before fetchAPIKeys)
      // This prevents double-clicks while table is refreshing
      onClose();
      onSuccess();
    } catch (err) {
      console.error('Failed to revoke API key:', err);
      setError(err instanceof Error ? err.message : 'Failed to revoke API key');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>Revoke API Key</DialogTitle>
          <DialogDescription>
            This action will immediately invalidate the API key
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>
              <strong>Warning:</strong> This action cannot be undone. Any applications using this key will no longer be able to access the API.
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
              Are you sure you want to revoke the API key <strong>"{keyName}"</strong>?
            </p>
            <p className="text-xs text-slate-500">
              The key will remain in the database for audit purposes but will no longer be usable.
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
            onClick={handleRevoke}
            disabled={loading}
          >
            {loading ? 'Revoking...' : 'Revoke API Key'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
