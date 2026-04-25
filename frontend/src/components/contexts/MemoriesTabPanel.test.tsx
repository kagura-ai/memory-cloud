/**
 * Tests for MemoriesTabPanel deep-link behavior (Issues #434 / #440).
 *
 * Covers:
 *   - ?memoryId= on mount auto-opens the dialog and renders ref data
 *   - referenceMemory rejection on a deep-link path renders the dialog in
 *     notFound mode (no toast)
 *   - URL is cleaned (memoryId param dropped) when the dialog closes
 *   - Backlink click hydrates the linked memory and updates the URL
 */

import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemoriesTabPanel } from "./MemoriesTabPanel";
import type { MemoryReference } from "@/lib/types/memory";

// ---------- Mocks ------------------------------------------------------------

const { mockReplace, mockSearchParams } = vi.hoisted(() => ({
  mockReplace: vi.fn(),
  mockSearchParams: { current: new URLSearchParams() },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace }),
  usePathname: () => "/workspace/contexts/ctx-1",
  useSearchParams: () => ({
    get: (k: string) => mockSearchParams.current.get(k),
    toString: () => mockSearchParams.current.toString(),
  }),
}));

const mockGetMemories = vi.fn();
const mockReferenceMemory = vi.fn();
vi.mock("@/lib/api/memory", () => ({
  getMemories: (...a: unknown[]) => mockGetMemories(...a),
  referenceMemory: (...a: unknown[]) => mockReferenceMemory(...a),
  forgetMemory: vi.fn(),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => (k: string) => k,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a", timezone: "UTC" } }),
}));

// ---------- Helpers ----------------------------------------------------------

const TARGET_ID = "11111111-1111-1111-1111-111111111111";

function makeRef(overrides: Partial<MemoryReference> = {}): MemoryReference {
  return {
    memory_id: TARGET_ID,
    summary: "Hydrated summary",
    context_summary: null,
    content: "Hydrated body",
    details: null,
    type: "note",
    scope: "working",
    importance: 0.7,
    tags: [],
    context: null,
    created_at: "2026-04-25T00:00:00Z",
    updated_at: "2026-04-25T00:00:00Z",
    client: "api",
    outgoing_links: [],
    outgoing_has_more: false,
    incoming_links: [],
    incoming_has_more: false,
    ...overrides,
  };
}

beforeEach(() => {
  mockReplace.mockReset();
  mockGetMemories.mockReset();
  mockReferenceMemory.mockReset();
  mockToast.mockReset();
  mockSearchParams.current = new URLSearchParams();
  // List endpoint always succeeds with one row by default — the panel must
  // not depend on the row being present for deep-link hydration.
  mockGetMemories.mockResolvedValue({
    memories: [
      {
        id: "99999999-9999-9999-9999-999999999999",
        summary: "Other row",
        type: "note",
        scope: "working",
        importance: 0.4,
        created_at: "2026-04-25T00:00:00Z",
        updated_at: "2026-04-25T00:00:00Z",
      },
    ],
    total: 1,
    has_more: false,
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

// ---------- Tests ------------------------------------------------------------

describe("MemoriesTabPanel — deep-link hydration (Issue #434)", () => {
  it("auto-opens the detail dialog when ?memoryId= is present on mount", async () => {
    mockSearchParams.current = new URLSearchParams(`memoryId=${TARGET_ID}`);
    mockReferenceMemory.mockResolvedValue(makeRef());

    render(<MemoriesTabPanel contextId="ctx-1" />);

    // Hydrated body should appear once referenceMemory resolves.
    await waitFor(() => {
      expect(mockReferenceMemory).toHaveBeenCalledWith(TARGET_ID);
    });
    await screen.findByText("Hydrated body");
  });

  it("renders the dialog in notFound mode when referenceMemory rejects on a URL path", async () => {
    mockSearchParams.current = new URLSearchParams(`memoryId=${TARGET_ID}`);
    mockReferenceMemory.mockRejectedValue(new Error("404"));

    render(<MemoriesTabPanel contextId="ctx-1" />);

    // The notFound title should appear; toast must NOT have been fired
    // (deep-link path renders EmptyState inside the dialog instead).
    // Title appears twice (sr-only DialogTitle + visible EmptyState h3),
    // so wait for any occurrence and assert ≥ 1.
    await screen.findAllByText("notFoundTitle");
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("drops the memoryId param when the dialog is closed", async () => {
    mockSearchParams.current = new URLSearchParams(`memoryId=${TARGET_ID}`);
    mockReferenceMemory.mockResolvedValue(makeRef());

    render(<MemoriesTabPanel contextId="ctx-1" />);

    await screen.findByText("Hydrated body");
    fireEvent.click(screen.getByRole("button", { name: "close" }));

    await waitFor(() => {
      // router.replace called without memoryId in the query string.
      const lastCall = mockReplace.mock.calls.at(-1)?.[0] as string;
      expect(lastCall).toBeDefined();
      expect(lastCall).not.toMatch(/memoryId=/);
    });
  });
});

describe("MemoriesTabPanel — backlink navigation (Issue #440 + #434)", () => {
  it("hydrates the linked memory and updates the URL when a backlink is clicked", async () => {
    const LINKED_ID = "55555555-5555-5555-5555-555555555555";
    mockSearchParams.current = new URLSearchParams(`memoryId=${TARGET_ID}`);

    // First call (initial deep-link): returns the source memory with one
    // outgoing link to LINKED_ID.
    mockReferenceMemory.mockResolvedValueOnce(
      makeRef({
        outgoing_links: [
          {
            memory_id: LINKED_ID,
            summary: "Click me",
            type: "note",
            importance: 0.5,
            weight: 1.0,
            created_at: "2026-04-25T00:00:00Z",
          },
        ],
      }),
    );
    // Second call (after backlink click): returns the linked memory.
    mockReferenceMemory.mockResolvedValueOnce(
      makeRef({
        memory_id: LINKED_ID,
        summary: "Linked memory",
        content: "Linked body",
      }),
    );

    render(<MemoriesTabPanel contextId="ctx-1" />);

    // Wait for source hydration so the References section renders.
    await screen.findByText("Click me");

    fireEvent.click(screen.getByRole("button", { name: /Click me/ }));

    await waitFor(() => {
      expect(mockReferenceMemory).toHaveBeenCalledWith(LINKED_ID);
    });

    // URL should have been updated to point at LINKED_ID.
    const writtenUrls = mockReplace.mock.calls.map((c) => c[0] as string);
    expect(writtenUrls.some((u) => u.includes(LINKED_ID))).toBe(true);
  });
});
