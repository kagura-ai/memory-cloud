/**
 * Tests for MemoryDetailDialog (Issues #443 / #441 / #440 / #434).
 *
 * Covers:
 *   - i18n: every user-visible string resolves through useTranslations (#443)
 *   - References section renders outgoing/incoming declared_link refs (#440)
 *   - References click invokes onOpenLinkedMemory (#440 + #434 composition)
 *   - References truncated hint renders when *HasMore is true (#440)
 *   - notFound mode renders an EmptyState body, not a hard 404 (#434)
 *   - References empty-state renders when both lists are empty arrays (#441)
 *   - References section stays hidden when both lists are undefined (#441)
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MemoryDetailDialog } from "./MemoryDetailDialog";
import type {
  LinkedMemoryRef,
  MemoryReference,
  SupersedeCandidate,
} from "@/lib/types/memory";

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

function makeRef(overrides: Partial<MemoryReference> = {}): MemoryReference {
  return {
    memory_id: "11111111-1111-1111-1111-111111111111",
    summary: "Sample summary",
    context_summary: null,
    content: "Sample body",
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

function makeCandidate(
  overrides: Partial<SupersedeCandidate> = {},
): SupersedeCandidate {
  return {
    memory_id: "99999999-9999-9999-9999-999999999999",
    summary: "Older duplicate memory",
    similarity: 0.91,
    detected_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

// ---------- Tests ------------------------------------------------------------

describe("MemoryDetailDialog — i18n", () => {
  it("uses translation keys for static labels and footer buttons", () => {
    render(
      <MemoryDetailDialog
        memory={makeRef()}
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
        memory={makeRef()}
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
        memory={makeRef()}
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
        memory={makeRef()}
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
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        outgoingLinks={[makeLink()]}
        outgoingHasMore={true}
      />,
    );

    expect(screen.getByText("references.truncated")).toBeInTheDocument();
  });

  it("renders the References empty-state when both lists are empty arrays (#441)", () => {
    render(
      <MemoryDetailDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        outgoingLinks={[]}
        incomingLinks={[]}
      />,
    );

    expect(screen.getByText("references.title")).toBeInTheDocument();
    expect(screen.getByText("references.emptyTitle")).toBeInTheDocument();
    expect(screen.getByText("references.emptyDesc")).toBeInTheDocument();
    expect(screen.queryByText("references.outgoing")).not.toBeInTheDocument();
    expect(screen.queryByText("references.incoming")).not.toBeInTheDocument();
  });

  it("hides the References section when both link props are undefined", () => {
    render(
      <MemoryDetailDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    expect(screen.queryByText("references.title")).not.toBeInTheDocument();
    expect(screen.queryByText("references.emptyTitle")).not.toBeInTheDocument();
  });

  it("does not render empty-state on partial fetch (#441)", () => {
    // Mixed-case: outgoing fetched (empty), incoming never queried. Claiming
    // "no incoming or outgoing links" would misrepresent the unfetched side,
    // so the empty-state must stay hidden until both directions resolve.
    render(
      <MemoryDetailDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        outgoingLinks={[]}
      />,
    );

    expect(screen.queryByText("references.emptyTitle")).not.toBeInTheDocument();
    expect(screen.queryByText("references.emptyDesc")).not.toBeInTheDocument();
  });
});

describe("MemoryDetailDialog — supersede suggestion (#1403/#1416)", () => {
  it("renders the suggestion block when a candidate is provided", () => {
    render(
      <MemoryDetailDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        supersedeCandidate={makeCandidate()}
        onAcceptSupersede={vi.fn()}
      />,
    );

    expect(screen.getByText("supersede.title")).toBeInTheDocument();
    expect(screen.getByText("supersede.description")).toBeInTheDocument();
    expect(screen.getByText("Older duplicate memory")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /supersede\.confirm/ }),
    ).toBeInTheDocument();
  });

  it("hides the suggestion block when no candidate is provided", () => {
    render(
      <MemoryDetailDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    expect(screen.queryByText("supersede.title")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /supersede\.confirm/ }),
    ).not.toBeInTheDocument();
  });

  it("invokes onAcceptSupersede when the confirm button is clicked", () => {
    const onAccept = vi.fn();
    render(
      <MemoryDetailDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        supersedeCandidate={makeCandidate()}
        onAcceptSupersede={onAccept}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /supersede\.confirm/ }));
    expect(onAccept).toHaveBeenCalledTimes(1);
  });

  it("disables the confirm button while accepting is in flight", () => {
    render(
      <MemoryDetailDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        supersedeCandidate={makeCandidate()}
        onAcceptSupersede={vi.fn()}
        supersedeAccepting={true}
      />,
    );

    expect(
      screen.getByRole("button", { name: /supersede\.confirm/ }),
    ).toBeDisabled();
  });

  it("opens the candidate memory when its summary row is clicked", () => {
    const onOpen = vi.fn();
    render(
      <MemoryDetailDialog
        memory={makeRef()}
        open={true}
        onOpenChange={vi.fn()}
        onDelete={vi.fn()}
        supersedeCandidate={makeCandidate()}
        onAcceptSupersede={vi.fn()}
        onOpenLinkedMemory={onOpen}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: /Older duplicate memory/ }),
    );
    expect(onOpen).toHaveBeenCalledWith("99999999-9999-9999-9999-999999999999");
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
