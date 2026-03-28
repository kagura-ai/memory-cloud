/**
 * View Schema Dialog Component
 *
 * Dialog for viewing schema details, version history, and exporting JSON
 * Issue #243 - Schema Management UI
 */

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Copy, Check, FileJson } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import type { ResourceSchema } from '@/lib/api/schemas';

interface ViewSchemaDialogProps {
  schema: ResourceSchema | null;
  isOpen: boolean;
  onClose: () => void;
}

export function ViewSchemaDialog({ schema, isOpen, onClose }: ViewSchemaDialogProps) {
  const t = useTranslations('schemas.viewDialog');
  const tCommon = useTranslations('common');

  const [copied, setCopied] = useState(false);
  const [showVersionHistory, setShowVersionHistory] = useState(false);

  if (!schema) return null;

  const handleCopyJSON = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(schema, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      if (process.env.NODE_ENV === 'development') {
        console.error('Failed to copy JSON:', err);
      }
    }
  };

  const formatDate = (isoString: string): string => {
    try {
      return formatDistanceToNow(new Date(isoString), { addSuffix: true });
    } catch {
      return isoString;
    }
  };

  const getClassificationBadge = (classification: string) => {
    const variants: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
      public: 'default',
      internal: 'secondary',
      pii: 'outline',
      confidential: 'destructive',
    };
    return variants[classification] || 'default';
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-4xl max-h-[90vh] flex flex-col">
        <DialogHeader>
          <DialogTitle>{t('title')}</DialogTitle>
          <DialogDescription>
            {schema.resource_id} v{schema.schema_version}
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center gap-2 mb-4">
          <code className="text-sm font-mono">{schema.resource_id}</code>
          <Badge variant="secondary">v{schema.schema_version}</Badge>
          <span className="text-xs text-muted-foreground">
            {t('created')}: {formatDate(schema.created_at)}
          </span>
        </div>

        <div className="flex-1 overflow-y-auto pr-4 max-h-[60vh]">
          <div className="space-y-6 py-4">
            {/* Field Definitions Table */}
            <div className="space-y-2">
              <h3 className="font-semibold">{t('fieldDefinitions')}</h3>
              <div className="border rounded-lg">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t('name')}</TableHead>
                      <TableHead>{t('type')}</TableHead>
                      <TableHead>{t('description')}</TableHead>
                      <TableHead>{t('classification')}</TableHead>
                      <TableHead>{t('indexHint')}</TableHead>
                      <TableHead>{t('required')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {schema.field_definitions.map((field, index) => (
                      <TableRow key={index}>
                        <TableCell>
                          <code className="text-sm font-mono">{field.name}</code>
                          {field.required && <span className="text-red-500 ml-1">*</span>}
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline">{field.type}</Badge>
                        </TableCell>
                        <TableCell className="max-w-xs">
                          <p className="text-sm text-muted-foreground truncate" title={field.description}>
                            {field.description}
                          </p>
                        </TableCell>
                        <TableCell>
                          <Badge variant={getClassificationBadge(field.classification)}>
                            {field.classification}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          {field.index_hint && (
                            <code className="text-xs text-muted-foreground">{field.index_hint}</code>
                          )}
                        </TableCell>
                        <TableCell>
                          {field.required && <Check className="h-4 w-4 text-green-500" />}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </div>

            {/* Version History Section */}
            <div className="space-y-2">
              <Button
                type="button"
                variant="outline"
                className="w-full"
                onClick={() => setShowVersionHistory(!showVersionHistory)}
              >
                {t('versionHistory')}
              </Button>
              {showVersionHistory && (
                <div className="mt-2 p-4 bg-muted rounded-md">
                  <p className="text-sm text-muted-foreground">
                    {t('currentVersion')}: v{schema.schema_version}
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {t('versionHistoryNote')}
                  </p>
                </div>
              )}
            </div>

            {/* JSON Export */}
            <div className="space-y-2">
              <Button
                variant="outline"
                onClick={handleCopyJSON}
                className="w-full"
              >
                {copied ? (
                  <>
                    <Check className="h-4 w-4 mr-2 text-green-500" />
                    {t('copied')}
                  </>
                ) : (
                  <>
                    <FileJson className="h-4 w-4 mr-2" />
                    {t('exportJSON')}
                  </>
                )}
              </Button>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button onClick={onClose}>{tCommon('close')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
