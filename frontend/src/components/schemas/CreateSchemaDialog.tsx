/**
 * Create Schema Dialog Component
 *
 * Dialog for creating a new resource schema with dynamic field definitions
 * Issue #243 - Schema Management UI
 */

import { useState, useEffect } from 'react';
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
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Plus, AlertCircle, Code } from 'lucide-react';
import { FieldDefinitionInput } from './FieldDefinitionInput';
import { getContexts } from '@/lib/api/contexts';
import {
  getSchema,  // P1-3: For existing schema check
  getResourceImpact,  // Issue #266: For impact warnings
  createSchema,
  getEmptyField,
  validateFields,
  type FieldDefinition,
  type ResourceSchema,
  type ResourceImpact,
} from '@/lib/api/schemas';
import type { Context } from '@/lib/types/context';
import { InlineSpinner } from '@/components/common/LoadingState';

interface CreateSchemaDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: (schema: ResourceSchema) => void;
}

export function CreateSchemaDialog({ isOpen, onClose, onSuccess }: CreateSchemaDialogProps) {
  const t = useTranslations('schemas.createDialog');
  const tCommon = useTranslations('common');

  // Form state
  const [resourceId, setResourceId] = useState('');
  const [fields, setFields] = useState<FieldDefinition[]>([getEmptyField()]);

  // UI state
  const [publicContexts, setPublicContexts] = useState<Context[]>([]);
  const [loadingContexts, setLoadingContexts] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPreview, setShowPreview] = useState(false);
  const [existingSchema, setExistingSchema] = useState<ResourceSchema | null>(null);

  // Issue #266: Impact information
  const [impact, setImpact] = useState<ResourceImpact | null>(null);
  const [loadingImpact, setLoadingImpact] = useState(false);

  // Load public contexts on dialog open
  useEffect(() => {
    if (isOpen) {
      loadPublicContexts();
      // Reset form
      setResourceId('');
      setFields([getEmptyField()]);
      setError(null);
      setShowPreview(false);
    }
  }, [isOpen]);

  const loadPublicContexts = async () => {
    try {
      setLoadingContexts(true);
      const data = await getContexts();
      const publicCtxs = data.contexts.filter((ctx) => ctx.is_public && ctx.resource_id);
      setPublicContexts(publicCtxs);
    } catch (err) {
      if (process.env.NODE_ENV === 'development') {
        console.error('Failed to load public contexts:', err);
      }
      setError(t('failedToLoadContexts'));
    } finally {
      setLoadingContexts(false);
    }
  };

  // Check for existing schema and impact when resource_id changes
  useEffect(() => {
    if (resourceId) {
      checkExistingSchema(resourceId);
      loadResourceImpact(resourceId);
    } else {
      setExistingSchema(null);
      setImpact(null);
    }
  }, [resourceId]);

  const checkExistingSchema = async (resId: string) => {
    try {
      const schema = await getSchema(resId);
      setExistingSchema(schema);
    } catch {
      // No existing schema (404) - this is fine
      setExistingSchema(null);
    }
  };

  // Issue #266: Load resource impact information
  const loadResourceImpact = async (resId: string) => {
    try {
      setLoadingImpact(true);
      const impactData = await getResourceImpact(resId);
      setImpact(impactData);
    } catch (err) {
      if (process.env.NODE_ENV === 'development') {
        console.error('Failed to load resource impact:', err);
      }
      // Not critical error - just don't show impact
      setImpact(null);
    } finally {
      setLoadingImpact(false);
    }
  };

  const handleAddField = () => {
    if (fields.length >= 100) {
      setError(t('fieldLimitReached'));
      return;
    }
    setFields([...fields, getEmptyField()]);
    // Auto-scroll to new field (in next tick)
    setTimeout(() => {
      const cards = document.querySelectorAll('[data-field-card]');
      const lastCard = cards[cards.length - 1];
      lastCard?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 100);
  };

  const handleUpdateField = (index: number, updates: Partial<FieldDefinition>) => {
    const newFields = [...fields];
    newFields[index] = { ...newFields[index], ...updates };
    setFields(newFields);
  };

  const handleRemoveField = (index: number) => {
    if (fields.length === 1) {
      setError(t('cannotRemoveLastField'));
      return;
    }

    // P1-4: Add confirmation for field deletion
    const fieldName = fields[index].name || t('unnamedField');
    if (!confirm(t('confirmRemoveField', { name: fieldName }))) {
      return;
    }

    setFields(fields.filter((_, i) => i !== index));
  };

  // P1-5: Prevent Enter key from submitting form
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && e.target instanceof HTMLInputElement) {
      e.preventDefault();
    }
  };

  const handleCreate = async () => {
    setError(null);

    // Validate resource_id
    if (!resourceId.trim()) {
      setError(t('resourceIdRequired'));
      return;
    }

    // Validate fields
    const fieldErrors = validateFields(fields);
    if (Object.keys(fieldErrors).length > 0) {
      setError(t('validationErrors'));
      return;
    }

    try {
      setCreating(true);
      const schema = await createSchema(resourceId, fields);
      onSuccess(schema);
      onClose();
    } catch (err) {
      if (process.env.NODE_ENV === 'development') {
        console.error('Failed to create schema:', err);
      }
      setError((err as any)?.message || t('createFailed'));
    } finally {
      setCreating(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-4xl max-h-[90vh] flex flex-col" onKeyDown={handleKeyDown}>
        <DialogHeader>
          <DialogTitle>{t('title')}</DialogTitle>
          <DialogDescription>{t('subtitle')}</DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto pr-4 max-h-[60vh]">
          <div className="space-y-6 py-4">
            {/* Resource ID Selection */}
            <div className="space-y-2">
              <Label htmlFor="resource-id">
                {t('resourceId')} <span className="text-red-500">*</span>
              </Label>
              <Select value={resourceId} onValueChange={setResourceId} disabled={loadingContexts}>
                <SelectTrigger id="resource-id">
                  <SelectValue placeholder={t('selectResourceId')} />
                </SelectTrigger>
                <SelectContent>
                  {publicContexts.map((ctx) => (
                    <SelectItem key={ctx.id} value={ctx.resource_id!}>
                      <div className="flex flex-col items-start">
                        <span className="font-medium">{ctx.resource_id}</span>
                        <span className="text-xs text-muted-foreground">
                          {ctx.display_name || ctx.name}
                        </span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {loadingContexts && (
                <div className="text-xs text-muted-foreground flex items-center gap-1">
                  <InlineSpinner /> {t('loadingContexts')}
                </div>
              )}
            </div>

            {/* Existing Schema Warning */}
            {existingSchema && (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>
                  {t('existingSchemaWarning', { version: existingSchema.schema_version })}
                </AlertDescription>
              </Alert>
            )}

            {/* Issue #266: Impact Warning */}
            {impact && !loadingImpact && (impact.token_count > 0 || impact.memory_count > 0) && (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>
                  <div className="space-y-2">
                    <p className="font-medium">
                      {t('impactWarningTitle', {
                        resourceId: impact.resource_id
                      })}
                    </p>
                    <ul className="list-disc list-inside text-sm space-y-1">
                      {impact.token_count > 0 && (
                        <li>
                          {t('activeTokens', { count: impact.token_count })}
                        </li>
                      )}
                      {impact.memory_count > 0 && (
                        <li>
                          {t('existingMemories', { count: impact.memory_count })}
                        </li>
                      )}
                    </ul>
                    <p className="text-sm mt-2">
                      {existingSchema
                        ? t('schemaChangeWarning')
                        : t('schemaCreationWarning')
                      }
                    </p>
                  </div>
                </AlertDescription>
              </Alert>
            )}

            {/* Field Definitions */}
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <Label className="text-base font-semibold">{t('fieldDefinitions')}</Label>
                <Button
                  type="button"
                  onClick={handleAddField}
                  size="sm"
                  variant="outline"
                  disabled={fields.length >= 100}
                >
                  <Plus className="h-4 w-4 mr-2" />
                  {t('addField')}
                </Button>
              </div>

              <div className="space-y-3" data-field-card>
                {fields.map((field, index) => {
                  const fieldErrors = validateFields(fields)[index] || {};
                  return (
                    <div key={index} data-field-card>
                      <FieldDefinitionInput
                        field={field}
                        index={index}
                        onUpdate={handleUpdateField}
                        onRemove={handleRemoveField}
                        errors={fieldErrors}
                        canRemove={fields.length > 1}
                      />
                    </div>
                  );
                })}
              </div>

              <p className="text-sm text-muted-foreground">
                {t('fieldCount', { current: fields.length, max: 100 })}
              </p>
            </div>

            {/* Preview Section */}
            <div className="space-y-2">
              <Button
                type="button"
                variant="outline"
                className="w-full"
                onClick={() => setShowPreview(!showPreview)}
              >
                <Code className="h-4 w-4 mr-2" />
                {t('preview')}
              </Button>
              {showPreview && (
                <div className="mt-2 p-4 bg-muted rounded-md">
                  <pre className="text-xs overflow-x-auto">
                    {JSON.stringify(
                      {
                        resource_id: resourceId,
                        field_definitions: fields,
                      },
                      null,
                      2
                    )}
                  </pre>
                </div>
              )}
            </div>

            {/* Error Alert */}
            {error && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={creating}>
            {tCommon('cancel')}
          </Button>
          <Button onClick={handleCreate} disabled={creating || !resourceId.trim()}>
            {creating && <InlineSpinner className="mr-2" />}
            {t('create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
