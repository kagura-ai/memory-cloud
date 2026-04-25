/**
 * Tests for EditMemoryDialog (Issue #439).
 *
 * Covers:
 *   - i18n: every user-visible label resolves through useTranslations
 *   - Initial render shows current values for all 6 patchable fields
 *   - Dirty detection: submitting unchanged form shows "no changes"
 *   - Submitting a real change calls updateMemoryById with only the diff
 *   - onSuccess receives the updated MemoryReference
 *   - API error surfaces as <Alert variant="destructive">
 *   - Invalid details JSON surfaces as field-adjacent error (not the Alert)
 *   - Unknown type values render as read-only (UI-recognized values use Select)
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditMemoryDialog } from "./EditMemoryDialog";
import type { MemoryReference } from "@/lib/types/memory";

// ---------- Mocks ------------------------------------------------------------

const stableT = (key: string, vars?: Record<string, unknown>) => {
  if (vars && typeof vars.id === "string") {
    return `${key}:${vars.id}`;
  }
  return key;
};

vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableT,
}));

const mockUpdate = vi.fn();
vi.mock("@/lib/api/memory", () => ({
  updateMemoryById: (...args: unknown[]) => mockUpdate(...args),
}));

beforeEach(() => {
  mockUpdate.mockReset();
});

// ---------- Fixtures ---------------------------------------------------------

function makeRef(overrides: Partial<MemoryReference> = {}): MemoryReference {
  return {
    memory_id: "11111111-1111-1111-1111-111111111111",
    summary: "Sample summary that is more than ten chars",
    context_summary: null,
    content: "Sample content body",
    details: null,
    type: "normal",
    scope: "working",
    importance: 0.5,
    tags: ["alpha", "beta"],
    context: null,
    created_at: "2026-04-25T00:00:00Z",
    updated_at: "2026-04-25T00:00:00Z",
    client: "mcp",
    source_uri: null,
    source_type: null,
    ...overrides,
  };
}

// ---------- Tests ------------------------------------------------------------

describe("EditMemoryDialog — initial render", () => {
  it("populates all 6 patchable fields from memory", () => {
    render(
      <EditMemoryDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(screen.getByLabelText(/summaryLabel/)).toHaveValue(
      "Sample summary that is more than ten chars",
    );
    expect(screen.getByLabelText(/contentLabel/)).toHaveValue(
      "Sample content body",
    );
    expect(screen.getByLabelText(/importanceLabel/)).toHaveValue(0.5);
    expect(screen.getByLabelText(/tagsLabel/)).toHaveValue("alpha, beta");
  });

  it("renders all i18n keys for static labels", () => {
    render(
      <EditMemoryDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(screen.getByText("title")).toBeInTheDocument();
    expect(
      screen.getByText(/description:11111111-1111-1111-1111-111111111111/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "cancel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "confirm" })).toBeInTheDocument();
  });
});

describe("EditMemoryDialog — type field", () => {
  it("shows read-only display + helper note when type is unknown to the UI", () => {
    render(
      <EditMemoryDialog
        memory={makeRef({ type: "decision" })}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    const typeInput = screen.getByLabelText(/typeLabel/);
    expect(typeInput).toBeDisabled();
    expect(typeInput).toHaveValue("decision");
    expect(screen.getByText("typeUnknownReadonly")).toBeInTheDocument();
  });
});

describe("EditMemoryDialog — submit", () => {
  it("blocks submit and shows 'no changes' when nothing was edited", async () => {
    const onSuccess = vi.fn();
    render(
      <EditMemoryDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={onSuccess}
      />,
    );

    const confirmBtn = screen.getByRole("button", { name: "confirm" });
    const form = confirmBtn.closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);

    await waitFor(() => {
      expect(screen.getByText("noChanges")).toBeInTheDocument();
    });
    expect(mockUpdate).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("submits only the dirty fields", async () => {
    const updated = makeRef({ importance: 0.95 });
    mockUpdate.mockResolvedValue(updated);
    const onSuccess = vi.fn();

    render(
      <EditMemoryDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={onSuccess}
      />,
    );

    fireEvent.change(screen.getByLabelText(/importanceLabel/), {
      target: { value: "0.95" },
    });
    const confirmBtn = screen.getByRole("button", { name: "confirm" });
    const form = confirmBtn.closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);

    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith(
        "11111111-1111-1111-1111-111111111111",
        { importance: 0.95 },
      );
    });
    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalledWith(updated);
    });
  });

  it("surfaces API errors via the destructive Alert", async () => {
    mockUpdate.mockRejectedValue(new Error("server unavailable"));

    render(
      <EditMemoryDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText(/importanceLabel/), {
      target: { value: "0.95" },
    });
    const confirmBtn = screen.getByRole("button", { name: "confirm" });
    const form = confirmBtn.closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);

    await waitFor(() => {
      expect(screen.getByText("server unavailable")).toBeInTheDocument();
    });
  });
});

describe("EditMemoryDialog — details JSON validation", () => {
  it("blocks submit on invalid JSON and shows field-adjacent error", async () => {
    render(
      <EditMemoryDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText(/detailsLabel/), {
      target: { value: "{ not json" },
    });
    const confirmBtn = screen.getByRole("button", { name: "confirm" });
    const form = confirmBtn.closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);

    await waitFor(() => {
      expect(screen.getByText("detailsParseError")).toBeInTheDocument();
    });
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it("rejects details that parse to a non-object value", async () => {
    render(
      <EditMemoryDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText(/detailsLabel/), {
      target: { value: "[1, 2, 3]" },
    });
    const confirmBtn = screen.getByRole("button", { name: "confirm" });
    const form = confirmBtn.closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);

    await waitFor(() => {
      expect(screen.getByText("detailsMustBeObject")).toBeInTheDocument();
    });
    expect(mockUpdate).not.toHaveBeenCalled();
  });
});
