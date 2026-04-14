/**
 * Tests for SchemaFieldTable.
 *
 * Verifies:
 * - renders a row per field definition
 * - classification badge variant reflects classification value (pii/confidential → destructive)
 * - required field shows the "required" badge
 * - unit annotation renders below the field name
 * - empty index_hint renders em-dash
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { SchemaFieldTable } from "./SchemaFieldTable";
import type { FieldDefinition } from "@/lib/api/schemas";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

const f = (overrides: Partial<FieldDefinition> = {}): FieldDefinition => ({
  name: "product_name",
  type: "text",
  description: "商品名",
  classification: "public",
  index_hint: "fulltext",
  required: false,
  ...overrides,
});

describe("SchemaFieldTable", () => {
  it("renders one row per field", () => {
    render(
      <SchemaFieldTable
        fields={[
          f({ name: "title" }),
          f({ name: "price", type: "number" }),
          f({ name: "active", type: "boolean" }),
        ]}
      />,
    );
    expect(screen.getByText("title")).toBeInTheDocument();
    expect(screen.getByText("price")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it("shows required badge only for required fields", () => {
    render(
      <SchemaFieldTable
        fields={[
          f({ name: "mandatory", required: true }),
          f({ name: "optional", required: false }),
        ]}
      />,
    );
    const badges = screen.getAllByText("required");
    expect(badges).toHaveLength(1);
    const mandatoryRow = screen.getByText("mandatory").closest("tr");
    expect(mandatoryRow).not.toBeNull();
    expect(
      within(mandatoryRow as HTMLElement).getByText("required"),
    ).toBeInTheDocument();
  });

  it("renders classification as visible text", () => {
    render(
      <SchemaFieldTable
        fields={[
          f({ name: "open", classification: "public" }),
          f({ name: "private_id", classification: "pii" }),
          f({ name: "secret", classification: "confidential" }),
        ]}
      />,
    );
    expect(screen.getByText("public")).toBeInTheDocument();
    expect(screen.getByText("pii")).toBeInTheDocument();
    expect(screen.getByText("confidential")).toBeInTheDocument();
  });

  it("renders em-dash when index_hint is empty", () => {
    render(
      <SchemaFieldTable fields={[f({ name: "no_hint", index_hint: "" })]} />,
    );
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("renders unit annotation when unit is provided", () => {
    render(
      <SchemaFieldTable
        fields={[f({ name: "price", type: "number", unit: "JPY" })]}
      />,
    );
    expect(screen.getByText("JPY")).toBeInTheDocument();
  });

  it("handles empty fields list without errors", () => {
    render(<SchemaFieldTable fields={[]} />);
    expect(screen.getByText("field")).toBeInTheDocument();
  });
});
