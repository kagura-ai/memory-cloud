/**
 * Schemas Table Component
 *
 * Table displaying list of resource schemas with actions
 * Issue #243 - Schema Management UI
 */

import { useState } from 'react';
import { useTranslations, useLocale } from 'next-intl';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Copy, Check } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { ja, enUS } from 'date-fns/locale';
import type { ResourceSchema } from '@/lib/api/schemas';

interface SchemasTableProps {
  schemas: ResourceSchema[];
  loading: boolean;
  onView: (schema: ResourceSchema) => void;
}

export function SchemasTable({ schemas, loading, onView }: SchemasTableProps) {
  const t = useTranslations('schemas.table');
  const locale = useLocale();
  const [copiedResourceId, setCopiedResourceId] = useState<string | null>(null);

  const handleCopyResourceId = async (resourceId: string) => {
    try {
      await navigator.clipboard.writeText(resourceId);
      setCopiedResourceId(resourceId);
      setTimeout(() => setCopiedResourceId(null), 2000);
    } catch (err) {
      if (process.env.NODE_ENV === 'development') {
        console.error('Failed to copy resource ID:', err);
      }
    }
  };

  const formatDate = (isoString: string): string => {
    try {
      const date = new Date(isoString);
      // Use relative time format from date-fns with locale support
      const dateFnsLocale = locale === 'ja' ? ja : enUS;
      return formatDistanceToNow(date, {
        addSuffix: true,
        locale: dateFnsLocale
      });
    } catch {
      return isoString;
    }
  };

  if (loading) {
    return (
      <div className="text-center py-8 text-muted-foreground">
        {t('loading')}
      </div>
    );
  }

  if (schemas.length === 0) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground mb-2">{t('noSchemas')}</p>
        <p className="text-sm text-muted-foreground">{t('noSchemasDescription')}</p>
      </div>
    );
  }

  return (
    <div className="border rounded-lg">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t('resourceId')}</TableHead>
            <TableHead>{t('version')}</TableHead>
            <TableHead>{t('fieldCount')}</TableHead>
            <TableHead>{t('created')}</TableHead>
            <TableHead className="text-right">{t('actions')}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {schemas.map((schema) => {
            const isCopied = copiedResourceId === schema.resource_id;

            return (
              <TableRow key={`${schema.resource_id}-v${schema.schema_version}`}>
                <TableCell>
                  <div className="flex items-center gap-2">
                    <code className="text-sm font-mono">{schema.resource_id}</code>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleCopyResourceId(schema.resource_id)}
                      className="h-6 w-6 p-0"
                    >
                      {isCopied ? (
                        <Check className="h-3 w-3 text-green-500" />
                      ) : (
                        <Copy className="h-3 w-3" />
                      )}
                    </Button>
                  </div>
                </TableCell>
                <TableCell>
                  <Badge variant="secondary">v{schema.schema_version}</Badge>
                </TableCell>
                <TableCell>
                  <span className="text-sm text-muted-foreground">
                    {schema.field_definitions.length} {t('fields')}
                  </span>
                </TableCell>
                <TableCell>
                  <span className="text-sm text-muted-foreground">{formatDate(schema.created_at)}</span>
                </TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="sm" onClick={() => onView(schema)}>
                    {t('view')}
                  </Button>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
