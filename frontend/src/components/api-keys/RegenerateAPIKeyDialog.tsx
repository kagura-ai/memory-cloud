/**
 * Regenerate API Key Dialog
 *
 * Issue #169 - Regenerate API key with confirmation
 * Shows new plaintext key ONLY once after regeneration
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
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AlertCircle, AlertTriangle, Check, Copy, RefreshCw } from 'lucide-react';
import { regenerateAPIKey } from '@/lib/api/api-keys';
import type { APIKeyCreateResponse } from '@/lib/types/api-key';
import { Alert, AlertDescription } from '@/components/ui/alert';

interface RegenerateAPIKeyDialogProps {
  isOpen: boolean;
  keyId: number;
  keyName: string;
  onClose: () => void;
  onSuccess: () => void;
}

export function RegenerateAPIKeyDialog({
  isOpen,
  keyId,
  keyName,
  onClose,
  onSuccess,
}: RegenerateAPIKeyDialogProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // New key display state
  const [regeneratedKey, setRegeneratedKey] = useState<APIKeyCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);

  const handleRegenerate = async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await regenerateAPIKey(keyId);

      // Show new key
      setRegeneratedKey(response);
    } catch (err) {
      console.error('Failed to regenerate API key:', err);
      const apiError = err as { message?: string; details?: { detail?: string } };
      const errorMessage = apiError.details?.detail || apiError.message || 'Failed to regenerate API key';
      setError(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = async () => {
    if (regeneratedKey) {
      await navigator.clipboard.writeText(regeneratedKey.api_key);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const handleClose = () => {
    setError(null);
    setRegeneratedKey(null);
    setCopied(false);
    onClose();
  };

  const handleDone = () => {
    handleClose();
    onSuccess();
  };

  // New key display mode
  if (regeneratedKey) {
    return (
      <Dialog open={isOpen} onOpenChange={handleClose}>
        <DialogContent className="sm:max-w-[600px]">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <RefreshCw className="h-5 w-5 text-green-600" />
              API Key Regenerated
            </DialogTitle>
            <DialogDescription>
              Save this new API key now. You won't be able to see it again!
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                <strong>Important:</strong> Copy this API key immediately. The old key has been invalidated.
              </AlertDescription>
            </Alert>

            <div className="space-y-2">
              <Label>Name</Label>
              <Input value={regeneratedKey.name} disabled />
            </div>

            <div className="space-y-2">
              <Label>New API Key (One-Time Display)</Label>
              <div className="flex gap-2">
                <Input
                  value={regeneratedKey.api_key}
                  disabled
                  className="font-mono text-sm"
                />
                <Button onClick={handleCopy} variant="outline">
                  {copied ? (
                    <>
                      <Check className="h-4 w-4 mr-2" />
                      Copied
                    </>
                  ) : (
                    <>
                      <Copy className="h-4 w-4 mr-2" />
                      Copy
                    </>
                  )}
                </Button>
              </div>
            </div>

            <div className="space-y-2">
              <Label>New Key Prefix (for identification)</Label>
              <code className="text-xs bg-slate-100 dark:bg-slate-800 px-3 py-2 rounded block">
                {regeneratedKey.key_prefix}...
              </code>
            </div>

            {regeneratedKey.expires_at && (
              <div className="space-y-2">
                <Label>Expires At</Label>
                <Input
                  value={new Date(regeneratedKey.expires_at).toLocaleString()}
                  disabled
                />
              </div>
            )}
          </div>

          <DialogFooter>
            <Button onClick={handleDone} className="w-full">
              {copied ? 'Done' : 'I have saved the new key'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  // Confirmation mode
  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-amber-500" />
            Regenerate API Key
          </DialogTitle>
          <DialogDescription>
            This will create a new key and invalidate the current one.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          {error && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <Alert variant="default" className="border-amber-200 bg-amber-50 dark:bg-amber-900/20">
            <AlertTriangle className="h-4 w-4 text-amber-600" />
            <AlertDescription className="text-amber-800 dark:text-amber-200">
              <strong>Warning:</strong> Regenerating this key will immediately invalidate the existing key.
              Any applications using it will stop working until updated with the new key.
            </AlertDescription>
          </Alert>

          <div className="space-y-2">
            <Label>Key Name</Label>
            <Input value={keyName} disabled />
          </div>
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={handleClose} disabled={loading}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="default"
            onClick={handleRegenerate}
            disabled={loading}
            className="bg-amber-600 hover:bg-amber-700 text-white"
          >
            {loading ? 'Regenerating...' : 'Regenerate Key'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
