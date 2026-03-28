/**
 * API Keys Table Component
 *
 * Issue #655 - Displays API keys in a table with action buttons
 * Issue #115 - Removed individual stats button (use Stats page instead)
 */

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Ban, RefreshCw, Trash2 } from 'lucide-react';
import type { APIKey } from '@/lib/types/api-key';
import { formatRelativeTime, getStatusColor } from '@/lib/api/api-keys';
import { SpinnerLoading } from '@/components/common/LoadingState';

interface APIKeysTableProps {
  apiKeys: APIKey[];
  loading: boolean;
  onRevoke: (key: APIKey) => void;
  onDelete: (key: APIKey) => void;
  onRegenerate?: (key: APIKey) => void;  // Issue #169
}

export function APIKeysTable({
  apiKeys,
  loading,
  onRevoke,
  onDelete,
  onRegenerate,
}: APIKeysTableProps) {
  const getStatusBadge = (status: string) => {
    const color = getStatusColor(status);
    const variant = color === 'green' ? 'default' : color === 'red' ? 'destructive' : 'secondary';

    return (
      <Badge variant={variant}>
        {status.charAt(0).toUpperCase() + status.slice(1)}
      </Badge>
    );
  };

  if (loading && apiKeys.length === 0) {
    return (
      <SpinnerLoading size="md" message="Loading API keys..." />
    );
  }

  if (!loading && apiKeys.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 border border-dashed rounded-lg">
        <div className="text-center">
          <p className="text-lg font-medium text-slate-900 dark:text-white">No API keys found</p>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Create your first API key to get started
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-800">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Key Prefix</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Created</TableHead>
            <TableHead>Last Used</TableHead>
            <TableHead>Expires</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {apiKeys.map((key) => (
            <TableRow key={key.id}>
              <TableCell className="font-medium">{key.name}</TableCell>
              <TableCell>
                <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded">
                  {key.key_prefix}...
                </code>
              </TableCell>
              <TableCell>{getStatusBadge(key.status)}</TableCell>
              <TableCell className="text-sm text-slate-500">
                {formatRelativeTime(key.created_at)}
              </TableCell>
              <TableCell className="text-sm text-slate-500">
                {formatRelativeTime(key.last_used_at)}
              </TableCell>
              <TableCell className="text-sm text-slate-500">
                {key.expires_at ? formatRelativeTime(key.expires_at) : 'Never'}
              </TableCell>
              <TableCell className="text-right">
                <div className="flex items-center justify-end gap-2">
                  {key.status === 'active' && onRegenerate && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onRegenerate(key)}
                      title="Regenerate API key"
                      className="text-amber-600 hover:text-amber-700 hover:bg-amber-50"
                    >
                      <RefreshCw className="h-4 w-4" />
                    </Button>
                  )}
                  {key.status === 'active' && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onRevoke(key)}
                      title="Revoke API key"
                      className="text-orange-600 hover:text-orange-700 hover:bg-orange-50"
                    >
                      <Ban className="h-4 w-4" />
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onDelete(key)}
                    title="Delete API key"
                    className="text-red-600 hover:text-red-700 hover:bg-red-50"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
