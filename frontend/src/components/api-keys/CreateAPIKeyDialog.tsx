/**
 * Create API Key Dialog
 *
 * Issue #655 - Create API key with expiration selection
 * Shows plaintext key ONLY once (one-time display)
 */

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/utils/clipboard";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { AlertCircle, Check, CheckCircle, Copy } from "lucide-react";
import { createAPIKey } from "@/lib/api/api-keys";
import { ApiError } from "@/lib/api/base";
import type { APIKeyCreateResponse } from "@/lib/types/api-key";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

interface CreateAPIKeyDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

export function CreateAPIKeyDialog({
  isOpen,
  onClose,
  onSuccess,
}: CreateAPIKeyDialogProps) {
  const [name, setName] = useState("");
  const [expiryDays, setExpiryDays] = useState<string>("null");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // One-time display state
  const [createdKey, setCreatedKey] = useState<APIKeyCreateResponse | null>(
    null,
  );
  const [copied, setCopied] = useState(false);
  // True when copyText exhausted both clipboard paths (#987). Surfaces an
  // in-dialog hint AND un-gates the Done button so a denied clipboard can't
  // trap the user behind "Copy the key first" with no way to proceed.
  const [copyFailed, setCopyFailed] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!name.trim()) {
      setError("Please enter a name for the API key");
      return;
    }

    try {
      setLoading(true);
      setError(null);

      const response = await createAPIKey({
        name: name.trim(),
        expires_days: expiryDays === "null" ? null : parseInt(expiryDays, 10),
      });

      // Show one-time display
      setCreatedKey(response);
    } catch (err) {
      console.error("Failed to create API key:", err);
      const apiError = err instanceof ApiError ? err : null;
      setError(
        apiError?.details?.detail ||
          (err instanceof Error ? err.message : "Failed to create API key"),
      );
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = async () => {
    if (createdKey) {
      try {
        // copyText degrades to an execCommand fallback before throwing
        // (issue #987), so a denied async-clipboard write no longer fails
        // silently. The key is shown in this dialog, so on hard failure the
        // user can still select + copy it manually.
        await copyText(createdKey.api_key);
        setCopied(true);
        setCopyFailed(false);
        setTimeout(() => setCopied(false), 2000);
      } catch (err) {
        console.error("Failed to copy API key:", err);
        setCopyFailed(true);
      }
    }
  };

  const handleClose = () => {
    setName("");
    setExpiryDays("null");
    setError(null);
    setCreatedKey(null);
    setCopied(false);
    setCopyFailed(false);
    onClose();
  };

  const handleDone = () => {
    handleClose();
    onSuccess();
  };

  // One-time display mode
  if (createdKey) {
    return (
      <Dialog open={isOpen} onOpenChange={handleClose}>
        <DialogContent className="sm:max-w-[600px]">
          <DialogHeader>
            <DialogTitle>API Key Created Successfully</DialogTitle>
            <DialogDescription>
              Save this API key now. You won't be able to see it again!
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <Alert className="border-green-200 bg-green-50">
              <CheckCircle className="h-4 w-4 text-green-500" />
              <AlertTitle className="text-green-800">Success!</AlertTitle>
              <AlertDescription className="text-green-700">
                Copy this API key immediately. It will only be shown once for
                security reasons.
              </AlertDescription>
            </Alert>

            <div className="space-y-4">
              {/* Name */}
              <div className="space-y-2">
                <Label>Name</Label>
                <Input value={createdKey.name} readOnly className="bg-white" />
              </div>

              {/* API Key */}
              <div className="space-y-2">
                <Label>API Key (One-Time Display)</Label>
                <div className="flex gap-2">
                  <Input
                    value={createdKey.api_key}
                    readOnly
                    className="font-mono text-sm bg-white"
                  />
                  <Button
                    onClick={handleCopy}
                    variant="outline"
                    className={copied ? "bg-green-50 border-green-300" : ""}
                  >
                    {copied ? (
                      <>
                        <Check className="h-4 w-4 text-green-600 mr-1" />
                        <span className="text-green-600 text-sm">Copied!</span>
                      </>
                    ) : (
                      <>
                        <Copy className="h-4 w-4 mr-1" />
                        <span className="text-sm">Copy</span>
                      </>
                    )}
                  </Button>
                </div>
                {copyFailed && (
                  <Alert variant="destructive">
                    <AlertCircle className="h-4 w-4" />
                    <AlertDescription>
                      Couldn&apos;t copy automatically. The key stays visible
                      above — select it and copy it manually.
                    </AlertDescription>
                  </Alert>
                )}
              </div>

              {/* Key Prefix */}
              <div className="space-y-2">
                <Label>Key Prefix (for identification)</Label>
                <code className="text-xs bg-slate-100 dark:bg-slate-800 px-3 py-2 rounded block">
                  {createdKey.key_prefix}...
                </code>
              </div>

              {/* Expires At */}
              {createdKey.expires_at && (
                <div className="space-y-2">
                  <Label>Expires At</Label>
                  <Input
                    value={new Date(createdKey.expires_at).toLocaleString()}
                    readOnly
                    className="bg-white"
                  />
                </div>
              )}

              <p className="text-xs text-slate-500">
                Store this key securely. You'll need it to authenticate API
                requests.
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button
              onClick={handleDone}
              className="w-full"
              disabled={!copied && !copyFailed}
            >
              {copied || copyFailed ? "Done" : "Copy the key first"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  // Create form mode
  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>Create API Key</DialogTitle>
          <DialogDescription>
            Generate a new API key for programmatic access to Kagura Memory API
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          <div className="space-y-4 py-4">
            {error && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            <div className="space-y-2">
              <Label htmlFor="name">Name *</Label>
              <Input
                id="name"
                placeholder="e.g., Production Server, CI/CD Pipeline"
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={loading}
                required
              />
              <p className="text-xs text-slate-500">
                A friendly name to identify this key
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="expiry">Expiration</Label>
              <Select
                value={expiryDays}
                onValueChange={setExpiryDays}
                disabled={loading}
              >
                <SelectTrigger id="expiry">
                  <SelectValue placeholder="Select expiration" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="30">30 days</SelectItem>
                  <SelectItem value="90">90 days</SelectItem>
                  <SelectItem value="365">1 year</SelectItem>
                  <SelectItem value="null">Never (no expiration)</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-slate-500">
                Key will be automatically invalidated after this period
              </p>
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
              {loading ? "Creating..." : "Create API Key"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
