/**
 * Tests for MemoryDetailDialog (Issues #443 / #440 / #434).
 *
 * Covers:
 *   - i18n: every user-visible string resolves through useTranslations (#443)
 *   - References section renders outgoing/incoming declared_link refs (#440)
 *   - References click invokes onOpenLinkedMemory (#440 + #434 composition)
 *   - References truncated hint renders when *HasMore is true (#440)
 *   - notFound mode renders an EmptyState body, not a hard 404 (#434)
 *   - References section is hidden when both lists are empty
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MemoryDetailDialog } from "./MemoryDetailDialog";
import type { LinkedMemoryRef, Memory } from "@/lib/types/memory";

// ---------- Mocks ------------------------------------------------------------

const stableT = (key: string, vars?: Record<string, unknown>) => {
  if (vars && typeof vars.summary === "string") {
    return `${key}:${vars.summary}`;
  }
  return key;
};

vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableT,
  useLocale: () => "en",
}));

const mockUseAuth = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

beforeEach(() => {
  mockUseAuth.mockReset().mockReturnValue({
    user: { id: "user-a", timezone: "UTC" },
  });
});

// ---------- Fixtures ---------------------------------------------------------

function makeMemory(overrides: Partial<Memory> = {}): Memory {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    summary: "Sample summary",
    key: "Sample summary",
    value: "Sample body",
    scope: "working",
    type: "note",
    agent_name: "",
    user_id: "user-a",
    importance: 0.5,
    tags: [],
    created_at: "2026-04-25T00:00:00Z",
    updated_at: "2026-04-25T00:00:00Z",
    ...overrides,
  };
}

function makeLink(overrides: Partial<LinkedMemoryRef> = {}): LinkedMemoryRef {
  return {
    memory_id: "22222222-2222-2222-2222-222222222222",
    summary: "Linked memory A",
    type: "note",
    importance: 0.6,
    weight: 1.0,
    created_at: "2026-04-25T00:00:00Z",
    ...overrides,
  };
}

// ---------- Tests ------------------------------------------------------------

describe("MemoryDetailDialog — i18n", () => {
  it("uses translation keys for static labels and footer buttons", () => {
    render(
      <MemoryDetailDialog
        memory={makeMemory()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    // Field labels — all should show translation keys (the stableT mock
    // returns the key verbatim), not the prior hardcoded English strings.
    expect(screen.getByText("memoryDetails")).toBeInTheDocument();
    expect(screen.getByText("value")).toBeInTheDocument();
    expect(screen.getByText("type")).toBeInTheDocument();
    expect(screen.getByText("importance")).toBeInTheDocument();
    expect(screen.getByText("createdAt")).toBeInTheDocument();
    expect(screen.getByText("updatedAt")).toBeInTheDocument();
    expect(screen.getByText("memoryId")).toBeInTheDocument();

    // Footer buttons.
    expect(screen.getByRole("button", { name: "close" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "delete" })).toBeInTheDocument();
  });

  it("does not render Edit when onEdit is not provided", () => {
    render(
      <MemoryDetailDialog
        memory={makeMemory()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "edit" }),
    ).not.toBeInTheDocument();
  });
});

describe("MemoryDetailDialog — References (Issue #440)", () => {
  it("renders outgoing and incoming sections when links are present", () => {
    const onOpen = vi.fn();
    render(
      <MemoryDetailDialog
        memory={makeMemory()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        outgoingLinks={[makeLink({ summary: "Out A" })]}
        incomingLinks={[
          makeLink({
            memory_id: "33333333-3333-3333-3333-333333333333",
            summary: "In A",
          }),
        ]}
        onOpenLinkedMemory={onOpen}
      />,
    );

    // Section heading + per-direction labels render via i18n keys.
    expect(screen.getByText("references.title")).toBeInTheDocument();
    expect(screen.getByText("references.outgoing")).toBeInTheDocument();
    expect(screen.getByText("references.incoming")).toBeInTheDocument();

    // Linked memory summaries render as button content.
    expect(screen.getByText("Out A")).toBeInTheDocument();
    expect(screen.getByText("In A")).toBeInTheDocument();
  });

  it("invokes onOpenLinkedMemory with the linked memory id on click", () => {
    const onOpen = vi.fn();
    render(
      <MemoryDetailDialog
        memory={makeMemory()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        outgoingLinks={[
          makeLink({
            memory_id: "44444444-4444-4444-4444-444444444444",
            summary: "Click me",
          }),
        ]}
        onOpenLinkedMemory={onOpen}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Click me/ }));
    expect(onOpen).toHaveBeenCalledWith("44444444-4444-4444-4444-444444444444");
  });

  it("shows the truncated hint when outgoingHasMore is true", () => {
    render(
      <MemoryDetailDialog
        memory={makeMemory()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        outgoingLinks={[makeLink()]}
        outgoingHasMore={true}
      />,
    );

    expect(screen.getByText("references.truncated")).toBeInTheDocument();
  });

  it("hides the References section when both lists are empty", () => {
    render(
      <MemoryDetailDialog
        memory={makeMemory()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        outgoingLinks={[]}
        incomingLinks={[]}
      />,
    );

    expect(screen.queryByText("references.title")).not.toBeInTheDocument();
  });
});

describe("MemoryDetailDialog — notFound mode (Issue #434)", () => {
  it("renders the EmptyState body when notFound=true", () => {
    render(
      <MemoryDetailDialog
        memory={null}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        notFound={true}
      />,
    );

    // notFoundTitle / notFoundDesc legitimately appear twice — once inside
    // the sr-only DialogTitle/Description (Radix accessibility) and once
    // inside the visible EmptyState. Match both with getAllByText.
    expect(screen.getAllByText("notFoundTitle").length).toBeGreaterThan(0);
    expect(screen.getAllByText("notFoundDesc").length).toBeGreaterThan(0);
    // Critical: we did NOT render the regular field labels — the dialog
    // is fully replaced by the EmptyState body.
    expect(screen.queryByText("memoryDetails")).not.toBeInTheDocument();
    expect(screen.queryByText("value")).not.toBeInTheDocument();
  });

  it("calls onOpenChange(false) when Close is clicked in notFound mode", () => {
    const onOpenChange = vi.fn();
    render(
      <MemoryDetailDialog
        memory={null}
        open={true}
        onOpenChange={onOpenChange}
        onDelete={vi.fn()}
        notFound={true}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
