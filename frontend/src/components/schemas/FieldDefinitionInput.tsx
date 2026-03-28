/**
 * Field Definition Input Component
 *
 * Card-based input for a single field definition with all metadata
 * Issue #243 - Schema Management UI
 */

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Checkbox } from '@/components/ui/checkbox';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { X, Plus } from 'lucide-react';
import type { FieldDefinition } from '@/lib/api/schemas';

interface FieldDefinitionInputProps {
  field: FieldDefinition;
  index: number;
  onUpdate: (index: number, field: Partial<FieldDefinition>) => void;
  onRemove: (index: number) => void;
  errors?: Record<string, string>;
  canRemove?: boolean;
}

const FIELD_TYPES = [
  { value: 'text', label: 'Text' },
  { value: 'number', label: 'Number' },
  { value: 'boolean', label: 'Boolean' },
  { value: 'date', label: 'Date' },
  { value: 'array', label: 'Array' },
  { value: 'object', label: 'Object' },
] as const;

const CLASSIFICATIONS = [
  { value: 'public', label: 'Public', colorClass: 'bg-green-500' },
  { value: 'internal', label: 'Internal', colorClass: 'bg-blue-500' },
  { value: 'pii', label: 'PII', colorClass: 'bg-orange-500' },
  { value: 'confidential', label: 'Confidential', colorClass: 'bg-red-500' },
] as const;

export function FieldDefinitionInput({
  field,
  index,
  onUpdate,
  onRemove,
  errors = {},
  canRemove = true,
}: FieldDefinitionInputProps) {
  const t = useTranslations('schemas.createDialog');
  const [enumInput, setEnumInput] = useState('');

  const handleChange = (key: keyof FieldDefinition, value: any) => {
    onUpdate(index, { [key]: value });
  };

  // Helper to translate error message
  const getErrorMessage = (errorKey: string): string => {
    if (errorKey.startsWith('duplicateFieldName:')) {
      const fieldName = errorKey.split(':')[1];
      return t('duplicateFieldName', { name: fieldName });
    }
    return t(errorKey as any);
  };

  const handleAddEnumValue = () => {
    const trimmed = enumInput.trim();
    if (!trimmed) return;

    const currentValues = field.enum_values || [];
    if (!currentValues.includes(trimmed)) {
      handleChange('enum_values', [...currentValues, trimmed]);
    }
    setEnumInput('');
  };

  const handleRemoveEnumValue = (valueToRemove: string) => {
    const currentValues = field.enum_values || [];
    handleChange(
      'enum_values',
      currentValues.filter((v) => v !== valueToRemove)
    );
  };

  return (
    <Card className="relative">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="font-medium">
            {t('fieldNumber', { number: index + 1 })}
          </div>
          {canRemove && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onRemove(index)}
              className="h-8 w-8 p-0"
            >
              <X className="h-4 w-4" />
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Row 1: Name + Type */}
        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label htmlFor={`field-${index}-name`}>
              {t('fieldName')} <span className="text-red-500">*</span>
            </Label>
            <Input
              id={`field-${index}-name`}
              value={field.name}
              onChange={(e) => handleChange('name', e.target.value)}
              placeholder="product_name"
              className={errors.name ? 'border-red-500' : ''}
            />
            {errors.name && <p className="text-xs text-red-500">{getErrorMessage(errors.name)}</p>}
            <p className="text-xs text-muted-foreground">{t('fieldNameHint')}</p>
          </div>

          <div className="space-y-2">
            <Label htmlFor={`field-${index}-type`}>
              {t('fieldType')} <span className="text-red-500">*</span>
            </Label>
            <Select value={field.type} onValueChange={(value) => handleChange('type', value)}>
              <SelectTrigger id={`field-${index}-type`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {FIELD_TYPES.map((type) => (
                  <SelectItem key={type.value} value={type.value}>
                    {type.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* Row 2: Description */}
        <div className="space-y-2">
          <Label htmlFor={`field-${index}-description`}>
            {t('description')} <span className="text-red-500">*</span>
          </Label>
          <Textarea
            id={`field-${index}-description`}
            value={field.description}
            onChange={(e) => handleChange('description', e.target.value)}
            placeholder={t('descriptionPlaceholder')}
            rows={2}
            className={errors.description ? 'border-red-500' : ''}
          />
          {errors.description && <p className="text-xs text-red-500">{getErrorMessage(errors.description)}</p>}
        </div>

        {/* Row 3: Classification + Index Hint */}
        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label htmlFor={`field-${index}-classification`}>{t('classification')}</Label>
            <Select
              value={field.classification}
              onValueChange={(value) => handleChange('classification', value)}
            >
              <SelectTrigger id={`field-${index}-classification`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CLASSIFICATIONS.map((cls) => (
                  <SelectItem key={cls.value} value={cls.value}>
                    <span className="flex items-center gap-2">
                      <span className={`inline-block h-2 w-2 rounded-full ${cls.colorClass}`} />
                      {cls.label}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor={`field-${index}-index-hint`}>{t('indexHint')}</Label>
            <Input
              id={`field-${index}-index-hint`}
              value={field.index_hint}
              onChange={(e) => handleChange('index_hint', e.target.value)}
              placeholder="fulltext, vector, sort, facet"
            />
            <p className="text-xs text-muted-foreground">{t('indexHintHelper')}</p>
          </div>
        </div>

        {/* Row 4: Unit + Required + Example */}
        <div className="grid grid-cols-3 gap-4">
          <div className="space-y-2">
            <Label htmlFor={`field-${index}-unit`}>{t('unit')}</Label>
            <Input
              id={`field-${index}-unit`}
              value={field.unit || ''}
              onChange={(e) => handleChange('unit', e.target.value || null)}
              placeholder="JPY, kg, %"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor={`field-${index}-example`}>{t('example')}</Label>
            <Input
              id={`field-${index}-example`}
              value={field.example || ''}
              onChange={(e) => handleChange('example', e.target.value || null)}
              placeholder={t('examplePlaceholder')}
            />
          </div>

          <div className="flex items-end pb-2">
            <div className="flex items-center space-x-2">
              <Checkbox
                id={`field-${index}-required`}
                checked={field.required}
                onCheckedChange={(checked) => handleChange('required', checked === true)}
              />
              <Label htmlFor={`field-${index}-required`} className="font-normal cursor-pointer">
                {t('required')}
              </Label>
            </div>
          </div>
        </div>

        {/* Row 5: Enum Values (Tag input) */}
        <div className="space-y-2">
          <Label>{t('enumValues')}</Label>
          <div className="flex gap-2">
            <Input
              value={enumInput}
              onChange={(e) => setEnumInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  handleAddEnumValue();
                }
              }}
              placeholder={t('enumValuesPlaceholder')}
            />
            <Button type="button" onClick={handleAddEnumValue} size="sm" variant="outline">
              <Plus className="h-4 w-4" />
            </Button>
          </div>
          {field.enum_values && field.enum_values.length > 0 && (
            <div className="flex flex-wrap gap-2 mt-2">
              {field.enum_values.map((value, i) => (
                <Badge key={i} variant="secondary" className="gap-1">
                  {value}
                  <button
                    onClick={() => handleRemoveEnumValue(value)}
                    className="ml-1 hover:text-red-500"
                    type="button"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
