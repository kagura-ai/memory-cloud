/**
 * Resource Schemas Client
 *
 * Functions for interacting with Resource Schema management endpoints
 * Issue #243 - Schema Management UI (Web-based)
 */

import { apiClient } from "./base";

/**
 * Validation error keys for type safety
 * High Priority Fix: Added type-safe validation error keys
 */
export type ValidationErrorKey =
  | "fieldNameRequired"
  | "fieldNamePattern"
  | "fieldNameTooLong"
  | "descriptionRequired";

/**
 * Field Definition interface
 * Defines metadata for a single field in the resource schema
 */
export interface FieldDefinition {
  name: string;
  type: "text" | "number" | "boolean" | "date" | "array" | "object";
  description: string;
  classification: "public" | "internal" | "pii" | "confidential";
  index_hint: string;
  unit?: string | null;
  enum_values?: string[] | null;
  example?: string | null;
  required: boolean;
}

/**
 * Resource Schema interface
 */
export interface ResourceSchema {
  resource_id: string;
  schema_version: number;
  field_definitions: FieldDefinition[];
  created_at: string;
}

/**
 * Resource Impact Response
 * Issue #266 - Schema change impact warnings
 */
export interface ResourceImpact {
  resource_id: string;
  token_count: number;
  memory_count: number;
  current_schema_version: number | null;
}

/**
 * Schema Creation Request
 */
export interface SchemaCreateRequest {
  resource_id: string;
  field_definitions: FieldDefinition[];
}

/**
 * Get resource schema (latest or specific version)
 *
 * @param resourceId - Resource identifier
 * @param schemaVersion - Optional schema version (default: latest)
 */
export async function getSchema(
  resourceId: string,
  schemaVersion?: number,
): Promise<ResourceSchema> {
  const searchParams = new URLSearchParams();
  if (schemaVersion) {
    searchParams.set("schema_version", schemaVersion.toString());
  }

  // encodeURIComponent protects against reserved chars in resource_id
  // (e.g., a literal `%` or `/`) that would otherwise break the path.
  const encodedId = encodeURIComponent(resourceId);
  const url = searchParams.toString()
    ? `/api/v1/resources/${encodedId}/schema?${searchParams.toString()}`
    : `/api/v1/resources/${encodedId}/schema`;

  return apiClient.get<ResourceSchema>(url);
}

/**
 * Get resource impact information
 * Issue #266 - Schema change impact warnings
 *
 * @param resourceId - Resource identifier
 */
export async function getResourceImpact(
  resourceId: string,
): Promise<ResourceImpact> {
  return apiClient.get<ResourceImpact>(
    `/api/v1/resources/${encodeURIComponent(resourceId)}/impact`,
  );
}

/**
 * Create a new resource schema
 *
 * @param resourceId - Resource identifier
 * @param fieldDefinitions - Array of field metadata
 */
export async function createSchema(
  resourceId: string,
  fieldDefinitions: FieldDefinition[],
): Promise<ResourceSchema> {
  return apiClient.post<ResourceSchema>(
    `/api/v1/resources/${encodeURIComponent(resourceId)}/schema`,
    {
      resource_id: resourceId,
      field_definitions: fieldDefinitions,
    },
  );
}

/**
 * Helper: Get empty field definition template
 */
export function getEmptyField(): FieldDefinition {
  return {
    name: "",
    type: "text",
    description: "",
    classification: "public",
    index_hint: "",
    unit: null,
    enum_values: null,
    example: null,
    required: false,
  };
}

/**
 * Helper: Validate field name
 * Returns translation key for error message (type-safe)
 */
export function validateFieldName(name: string): ValidationErrorKey | null {
  if (!name.trim()) {
    return "fieldNameRequired";
  }

  const pattern = /^[a-z0-9_]+$/;
  if (!pattern.test(name)) {
    return "fieldNamePattern";
  }

  if (name.length > 100) {
    return "fieldNameTooLong";
  }

  return null;
}

/**
 * Helper: Check for duplicate field names
 */
export function findDuplicateFieldNames(fields: FieldDefinition[]): string[] {
  const names = fields.map((f) => f.name.trim()).filter((n) => n.length > 0);
  const duplicates = names.filter(
    (name, index) => names.indexOf(name) !== index,
  );
  return [...new Set(duplicates)];
}

/**
 * Helper: Validate all fields
 * Returns translation keys for error messages (type-safe where possible)
 */
export function validateFields(
  fields: FieldDefinition[],
): Record<number, Record<string, ValidationErrorKey | string>> {
  const errors: Record<
    number,
    Record<string, ValidationErrorKey | string>
  > = {};

  fields.forEach((field, index) => {
    const fieldErrors: Record<string, ValidationErrorKey | string> = {};

    const nameError = validateFieldName(field.name);
    if (nameError) {
      fieldErrors.name = nameError;
    }

    if (!field.description.trim()) {
      fieldErrors.description = "descriptionRequired" as ValidationErrorKey;
    }

    if (Object.keys(fieldErrors).length > 0) {
      errors[index] = fieldErrors;
    }
  });

  // Check for duplicates
  const duplicates = findDuplicateFieldNames(fields);
  if (duplicates.length > 0) {
    fields.forEach((field, index) => {
      if (duplicates.includes(field.name.trim())) {
        errors[index] = errors[index] || {};
        // Store both key and field name for translation
        errors[index].name = `duplicateFieldName:${field.name}`;
      }
    });
  }

  return errors;
}
