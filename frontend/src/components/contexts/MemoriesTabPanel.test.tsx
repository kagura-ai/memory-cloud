/**
 * Tests for MemoriesTabPanel deep-link behavior (Issues #434 / #440)
 * and debounced ?q= search (Issue #580).
 *
 * Covers:
 *   - ?memoryId= on mount auto-opens the dialog and renders ref data
 *   - referenceMemory rejection on a deep-link path renders the dialog in
 *     notFound mode (no toast)
 *   - URL is cleaned (memoryId param dropped) when the dialog closes
 *   - Backlink click hydrates the linked memory and updates the URL
 *   - Typing the search input fires a debounced fetch carrying `q`
 *   - Clearing the input drops `q` from the next fetch and the URL
 *   - URL is updated with ?q= for shareable links
 *   - Changing the query resets pagination to page 1
 *   - No-results empty state quotes the query text back at the user
 */

import {
  act,
  render,
  screen,
  waitFor,
  fireEvent,
} from "@testing-library/react";
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

// `t(key)` returns the key verbatim, `t(key, { query: "foo" })` returns
// `<key>:foo` so query-echoing copy stays assertable without dragging in
// the real ICU compiler. Identity is stable across renders so `useCallback`
// deps that close over `t` don't churn (and we don't get spurious refetches
// on every keystroke).
const { stableT } = vi.hoisted(() => ({
  stableT: (k: string, values?: Record<string, unknown>) => {
    if (!values) return k;
    const parts = Object.values(values).map((v) => String(v));
    return parts.length > 0 ? `${k}:${parts.join(",")}` : k;
  },
}));
vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableT,
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

// ---------------------------------------------------------------------------
// Debounced search (Issue #580)
// ---------------------------------------------------------------------------

describe("MemoriesTabPanel — debounced search (Issue #580)", () => {
  // Locate the search input by its translated aria-label key — the i18n
  // mock returns the key string verbatim, so this is stable across copy
  // changes.
  const getSearchInput = (): HTMLInputElement =>
    screen.getByLabelText("search.label") as HTMLInputElement;

  // Pull the most recent call's `q` value (undefined if omitted entirely).
  const lastFetchQ = (): string | undefined => {
    const lastArgs = mockGetMemories.mock.calls.at(-1)?.[0] as
      | { q?: string }
      | undefined;
    return lastArgs?.q;
  };

  it("renders the search input above the table on initial paint", async () => {
    render(<MemoriesTabPanel contextId="ctx-1" />);

    // Input is present immediately, even before the first fetch resolves.
    expect(getSearchInput()).toBeInTheDocument();
    // And the initial fetch carries no q.
    await waitFor(() => expect(mockGetMemories).toHaveBeenCalled());
    expect(lastFetchQ()).toBeUndefined();
  });

  it("seeds the input from ?q= on mount and forwards it on the first fetch", async () => {
    mockSearchParams.current = new URLSearchParams("q=foo");

    render(<MemoriesTabPanel contextId="ctx-1" />);

    expect(getSearchInput().value).toBe("foo");
    await waitFor(() => expect(mockGetMemories).toHaveBeenCalled());
    expect(lastFetchQ()).toBe("foo");
  });

  it("debounces keystrokes — only the trailing value triggers a q-bearing fetch", async () => {
    vi.useFakeTimers();
    try {
      render(<MemoriesTabPanel contextId="ctx-1" />);

      // Drain the initial mount fetch.
      await vi.waitFor(() => expect(mockGetMemories).toHaveBeenCalledTimes(1));
      const baselineCalls = mockGetMemories.mock.calls.length;

      // Type a 3-char query in quick succession (well under 300ms).
      fireEvent.change(getSearchInput(), { target: { value: "h" } });
      act(() => {
        vi.advanceTimersByTime(100);
      });
      fireEvent.change(getSearchInput(), { target: { value: "he" } });
      act(() => {
        vi.advanceTimersByTime(100);
      });
      fireEvent.change(getSearchInput(), { target: { value: "hel" } });

      // Mid-stream: no extra fetch yet — debounce hasn't elapsed.
      expect(mockGetMemories.mock.calls.length).toBe(baselineCalls);

      // Cross the 300ms threshold from the LAST keystroke.
      act(() => {
        vi.advanceTimersByTime(300);
      });

      await vi.waitFor(() =>
        expect(mockGetMemories.mock.calls.length).toBeGreaterThan(
          baselineCalls,
        ),
      );
      expect(lastFetchQ()).toBe("hel");
    } finally {
      vi.useRealTimers();
    }
  });

  it("clearing the input drops q from the next fetch", async () => {
    vi.useFakeTimers();
    try {
      mockSearchParams.current = new URLSearchParams("q=foo");

      render(<MemoriesTabPanel contextId="ctx-1" />);

      await vi.waitFor(() => expect(mockGetMemories).toHaveBeenCalled());
      expect(lastFetchQ()).toBe("foo");

      fireEvent.change(getSearchInput(), { target: { value: "" } });
      act(() => {
        vi.advanceTimersByTime(300);
      });

      await vi.waitFor(() => expect(lastFetchQ()).toBeUndefined());
    } finally {
      vi.useRealTimers();
    }
  });

  it("syncs ?q= into the URL via router.replace after the debounce", async () => {
    vi.useFakeTimers();
    try {
      render(<MemoriesTabPanel contextId="ctx-1" />);
      await vi.waitFor(() => expect(mockGetMemories).toHaveBeenCalled());

      fireEvent.change(getSearchInput(), { target: { value: "alpha" } });
      // Pre-debounce: no URL write for the query.
      const preCalls = mockReplace.mock.calls.length;
      expect(
        mockReplace.mock.calls.slice(0, preCalls).some((c) => {
          const url = c[0] as string;
          return /[?&]q=alpha/.test(url);
        }),
      ).toBe(false);

      act(() => {
        vi.advanceTimersByTime(300);
      });

      await vi.waitFor(() => {
        const urls = mockReplace.mock.calls.map((c) => c[0] as string);
        expect(urls.some((u) => /[?&]q=alpha/.test(u))).toBe(true);
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("strips ?q= from the URL when the input is cleared", async () => {
    vi.useFakeTimers();
    try {
      mockSearchParams.current = new URLSearchParams("q=foo");

      render(<MemoriesTabPanel contextId="ctx-1" />);
      await vi.waitFor(() => expect(mockGetMemories).toHaveBeenCalled());

      fireEvent.change(getSearchInput(), { target: { value: "" } });
      act(() => {
        vi.advanceTimersByTime(300);
      });

      await vi.waitFor(() => {
        const lastUrl = mockReplace.mock.calls.at(-1)?.[0] as string;
        expect(lastUrl).toBeDefined();
        expect(lastUrl).not.toMatch(/[?&]q=/);
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("resets pagination to page 1 when the debounced query changes", async () => {
    // Seed a multi-page result so the Next button is rendered & enabled.
    mockGetMemories.mockResolvedValue({
      memories: [
        {
          id: "99999999-9999-9999-9999-999999999999",
          summary: "Some row",
          type: "note",
          scope: "working",
          importance: 0.4,
          created_at: "2026-04-25T00:00:00Z",
          updated_at: "2026-04-25T00:00:00Z",
        },
      ],
      total: 120,
      has_more: true,
    });

    render(<MemoriesTabPanel contextId="ctx-1" />);

    // Wait for the table to render — paginationNext only appears once
    // loading resolves and the table is mounted.
    const nextBtn = await screen.findByRole("button", {
      name: "paginationNext",
    });
    fireEvent.click(nextBtn);

    // The second fetch should carry offset=50 (page 2).
    await waitFor(() => {
      const lastArgs = mockGetMemories.mock.calls.at(-1)?.[0] as {
        offset: number;
      };
      expect(lastArgs.offset).toBe(50);
    });

    // Now switch to fake timers for the debounced typing — but we already
    // have the post-page-2 fetch in hand, so the new fetch comparison is
    // safe.
    vi.useFakeTimers();
    try {
      fireEvent.change(getSearchInput(), { target: { value: "needle" } });
      act(() => {
        vi.advanceTimersByTime(300);
      });

      await vi.waitFor(() => {
        const lastArgs = mockGetMemories.mock.calls.at(-1)?.[0] as {
          offset: number;
          q?: string;
        };
        expect(lastArgs.q).toBe("needle");
        expect(lastArgs.offset).toBe(0);
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders the no-results empty state with the query when nothing matches", async () => {
    // Start with a populated context so the empty-context branch doesn't
    // hijack the render — we only want to exercise the no-results branch.
    render(<MemoriesTabPanel contextId="ctx-1" />);
    await waitFor(() => expect(mockGetMemories).toHaveBeenCalled());

    // Now flip the mock to return an empty set for the upcoming search.
    mockGetMemories.mockResolvedValue({
      memories: [],
      total: 0,
      has_more: false,
    });

    vi.useFakeTimers();
    try {
      fireEvent.change(getSearchInput(), { target: { value: "zzz" } });
      act(() => {
        vi.advanceTimersByTime(300);
      });
    } finally {
      vi.useRealTimers();
    }

    // i18n mock encodes "{key}:{value}" — the description must contain
    // the query value.
    await screen.findByText(/noResultsDesc:zzz/);
    // And the no-results title is used, not the zero-memories title.
    expect(screen.queryByText("emptyTitle")).not.toBeInTheDocument();
    expect(screen.getByText("noResultsTitle")).toBeInTheDocument();
  });

  it("whitespace-only input is treated as absent (q omitted, no URL noise)", async () => {
    vi.useFakeTimers();
    try {
      render(<MemoriesTabPanel contextId="ctx-1" />);
      await vi.waitFor(() => expect(mockGetMemories).toHaveBeenCalled());

      fireEvent.change(getSearchInput(), { target: { value: "   " } });
      act(() => {
        vi.advanceTimersByTime(300);
      });

      // Whitespace-only debouncedQuery is trimmed inside the component
      // before the fetch call — q must be undefined on the wire (a server
      // that received `"   "` would be a regression).
      await vi.waitFor(() => {
        const lastArgs = mockGetMemories.mock.calls.at(-1)?.[0] as {
          q?: string;
        };
        expect(lastArgs.q).toBeUndefined();
      });

      // And no URL was written with `q=` set to a whitespace-only value.
      const urls = mockReplace.mock.calls.map((c) => c[0] as string);
      expect(urls.some((u) => /[?&]q=(?:%20|\s)/.test(u))).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});
