/**
 * Schema Field Table
 *
 * Renders a resource's field_definitions as a table.
 * Used inside the Overview tab of the Resource Detail page.
 *
 * Issue #47
 */

"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { FieldDefinition } from "@/lib/api/schemas";

interface SchemaFieldTableProps {
  fields: FieldDefinition[];
}

function classificationVariant(
  classification: FieldDefinition["classification"],
): "default" | "secondary" | "destructive" | "outline" {
  switch (classification) {
    case "pii":
    case "confidential":
      return "destructive";
    case "internal":
      return "secondary";
    case "public":
    default:
      return "outline";
  }
}

export function SchemaFieldTable({ fields }: SchemaFieldTableProps) {
  const t = useTranslations("resources.schema");

  return (
    <div className="rounded-lg border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("field")}</TableHead>
            <TableHead>{t("type")}</TableHead>
            <TableHead>{t("classification")}</TableHead>
            <TableHead>{t("indexHint")}</TableHead>
            <TableHead>{t("description")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {fields.map((field) => (
            <TableRow key={field.name}>
              <TableCell className="font-mono text-sm">
                <div className="flex items-center gap-2">
                  <span>{field.name}</span>
                  {field.required && (
                    <Badge variant="outline" className="text-xs">
                      {t("required")}
                    </Badge>
                  )}
                </div>
                {field.unit && (
                  <div className="text-xs text-muted-foreground mt-0.5">
                    {field.unit}
                  </div>
                )}
              </TableCell>
              <TableCell>
                <Badge variant="secondary" className="font-mono">
                  {field.type}
                </Badge>
              </TableCell>
              <TableCell>
                <Badge variant={classificationVariant(field.classification)}>
                  {field.classification}
                </Badge>
              </TableCell>
              <TableCell className="font-mono text-xs text-muted-foreground">
                {field.index_hint || "—"}
              </TableCell>
              <TableCell className="text-sm text-muted-foreground">
                {field.description}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
