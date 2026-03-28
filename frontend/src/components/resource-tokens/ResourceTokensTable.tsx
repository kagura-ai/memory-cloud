/**
 * Resource Tokens Table Component
 *
 * Issue #242 - Displays resource tokens in a table with action buttons
 */

import { useTranslations } from 'next-intl';
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
import { Trash2, Edit, ExternalLink, Copy, Check } from 'lucide-react';
import Link from 'next/link';
import { useState } from 'react';
import type { ResourceToken } from '@/lib/api/resource-tokens';
import { formatRelativeTime, getStatusColor } from '@/lib/api/resource-tokens';
import { SpinnerLoading } from '@/components/common/LoadingState';

interface ResourceTokensTableProps {
  tokens: ResourceToken[];
  contexts?: any[];
  loading: boolean;
  onRevoke: (token: ResourceToken) => void;
  onEdit?: (token: ResourceToken) => void;
}

export function ResourceTokensTable({
  tokens,
  contexts = [],
  loading,
  onRevoke,
  onEdit,
}: ResourceTokensTableProps) {
  const t = useTranslations('resourceTokens');
  const [copiedTokenId, setCopiedTokenId] = useState<number | null>(null);  // Use token.id, not resource_id

  const formatTime = (isoString: string | null): string => {
    if (!isoString) return t('timeAgo.never');

    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    const diffHour = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHour / 24);

    if (diffDay > 0) {
      return t('timeAgo.daysAgo', { days: diffDay });
    } else if (diffHour > 0) {
      return t('timeAgo.hoursAgo', { hours: diffHour });
    } else if (diffMin > 0) {
      return t('timeAgo.minutesAgo', { minutes: diffMin });
    } else {
      return t('timeAgo.justNow');
    }
  };

  const handleCopyResourceId = async (tokenId: number, resourceId: string) => {
    try {
      await navigator.clipboard.writeText(resourceId);
      setCopiedTokenId(tokenId);  // Track by token.id to avoid N:1 collision
      setTimeout(() => setCopiedTokenId(null), 2000);
    } catch (err) {
      // Silently fail
    }
  };

  const getStatusBadge = (status: 'active' | 'revoked') => {
    const color = getStatusColor(status);
    const variant = color === 'green' ? 'default' : 'secondary';
    const label = status === 'active' ? t('table.active') : t('table.revoked');

    return (
      <Badge variant={variant}>
        {label}
      </Badge>
    );
  };

  if (loading && tokens.length === 0) {
    return (
      <SpinnerLoading size="md" message={t('loading')} />
    );
  }

  if (!loading && tokens.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 border border-dashed rounded-lg">
        <div className="text-center">
          <p className="text-lg font-medium text-slate-900 dark:text-white">{t('noTokens')}</p>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            {t('noTokensDescription')}
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
            <TableHead>{t('table.resourceId')}</TableHead>
            <TableHead>{t('table.description')}</TableHead>
            <TableHead>{t('table.quota')}</TableHead>
            <TableHead>{t('table.status')}</TableHead>
            <TableHead>{t('table.created')}</TableHead>
            <TableHead>{t('table.lastUsed')}</TableHead>
            <TableHead className="text-right">{t('table.actions')}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {tokens.map((token) => {
            const context = contexts.find(c => c.resource_id === token.resource_id);
            return (
            <TableRow key={token.id}>
              <TableCell className="font-medium">
                <div className="flex items-center gap-2">
                  <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded">
                    {token.resource_id}
                  </code>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 w-6 p-0"
                    onClick={() => handleCopyResourceId(token.id, token.resource_id)}
                    title="Copy resource ID"
                  >
                    {copiedTokenId === token.id ? (
                      <Check className="h-3 w-3 text-green-600" />
                    ) : (
                      <Copy className="h-3 w-3 text-slate-400" />
                    )}
                  </Button>
                  {context && (
                    <Link
                      href={`/workspace/contexts/${context.id}/stats`}
                      className="text-blue-600 hover:text-blue-700"
                      title="View context usage stats"
                    >
                      <ExternalLink className="h-3 w-3" />
                    </Link>
                  )}
                </div>
              </TableCell>
              <TableCell className="text-sm text-slate-600 dark:text-slate-400">
                {token.description || <span className="italic text-slate-400">{t('table.noDescription')}</span>}
              </TableCell>
              <TableCell className="text-sm font-mono">
                {token.quota_events_per_hour.toLocaleString()}
              </TableCell>
              <TableCell>{getStatusBadge(token.status)}</TableCell>
              <TableCell className="text-sm text-slate-500">
                {formatTime(token.created_at)}
              </TableCell>
              <TableCell className="text-sm text-slate-500">
                {formatTime(token.last_used_at)}
              </TableCell>
              <TableCell className="text-right">
                <div className="flex items-center justify-end gap-2">
                  {token.status === 'active' && onEdit && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onEdit(token)}
                      title="Edit token"
                      className="text-blue-600 hover:text-blue-700 hover:bg-blue-50"
                    >
                      <Edit className="h-4 w-4" />
                    </Button>
                  )}
                  {token.status === 'active' && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onRevoke(token)}
                      title="Revoke resource token"
                      className="text-red-600 hover:text-red-700 hover:bg-red-50"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  )}
                  {token.status === 'revoked' && (
                    <span className="text-xs text-slate-400 italic">{t('table.revoked')}</span>
                  )}
                </div>
              </TableCell>
            </TableRow>
          )}
          )}
        </TableBody>
      </Table>
    </div>
  );
}
