/**
 * Tests for DeleteMemoryDialog (Issue #446).
 *
 * Covers:
 *   - renders preview block (summary, id, scope, content excerpt) using MemoryReference
 *   - confirm button fires forgetMemory + onSuccess with the memory_id
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DeleteMemoryDialog } from "./DeleteMemoryDialog";
import type { MemoryReference } from "@/lib/types/memory";

// ---------- Mocks ------------------------------------------------------------

const stableT = (key: string) => key;

vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableT,
}));

const mockForget = vi.fn();
vi.mock("@/lib/api/memory", () => ({
  forgetMemory: (...args: unknown[]) => mockForget(...args),
}));

beforeEach(() => {
  mockForget.mockReset();
});

// ---------- Fixtures ---------------------------------------------------------

function makeRef(overrides: Partial<MemoryReference> = {}): MemoryReference {
  return {
    memory_id: "11111111-1111-1111-1111-111111111111",
    summary: "Sample summary",
    context_summary: null,
    content: "Sample content body",
    details: null,
    type: "note",
    scope: "working",
    importance: 0.5,
    tags: [],
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

describe("DeleteMemoryDialog — preview", () => {
  it("renders summary, memory_id, scope, and content preview from MemoryReference", () => {
    render(
      <DeleteMemoryDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(screen.getByText("Sample summary")).toBeInTheDocument();
    expect(
      screen.getByText("11111111-1111-1111-1111-111111111111"),
    ).toBeInTheDocument();
    expect(screen.getByText("working")).toBeInTheDocument();
    expect(screen.getByText(/Sample content body/)).toBeInTheDocument();
  });
});

describe("DeleteMemoryDialog — confirm", () => {
  it("invokes forgetMemory with memory_id and fires onSuccess on confirm", async () => {
    mockForget.mockResolvedValue(undefined);
    const onSuccess = vi.fn();

    render(
      <DeleteMemoryDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onSuccess={onSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "confirm" }));

    await waitFor(() => {
      expect(mockForget).toHaveBeenCalledWith(
        "11111111-1111-1111-1111-111111111111",
      );
    });
    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalled();
    });
  });
});
