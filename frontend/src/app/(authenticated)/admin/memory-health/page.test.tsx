/**
 * Tests for the Admin Memory Health page (#1225 Phase 2).
 *
 * Pins the per-context contract at the UI layer:
 *   - the breakdown renders one row per entry, naming each context (the
 *     unattributed bucket gets its localized label, never a blank)
 *   - drill-down fetches ?context_id=<uuid> (or the 'unattributed'
 *     sentinel) and renders the 3-section detail
 *   - structured notes render via their message-catalog code with params
 *     interpolated; unknown codes fall back to the generic message
 *     (never crash, never blank)
 *   - zero contexts renders the empty state, not an error
 */

import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";

import AdminMemoryHealthPage from "./page";

// ---------- Mocks ------------------------------------------------------------

const mockGet = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...original,
    apiClient: {
      get: (...args: unknown[]) => mockGet(...args),
    },
  };
});

// Stateful navigation mock (#1227): a tiny history store the mocked
// useSearchParams subscribes to via useSyncExternalStore, so router.push
// re-renders the page exactly like a real URL change, and browser
// back/forward are simulated by moving the history index inside act().
const { navMock } = vi.hoisted(() => {
  let entries: string[] = [""];
  let idx = 0;
  const listeners = new Set<() => void>();
  const notify = () => listeners.forEach((l) => l());
  const navMock = {
    getQs: () => entries[idx],
    subscribe: (cb: () => void) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    push(url: string) {
      const q = url.indexOf("?");
      entries = entries.slice(0, idx + 1).concat(q === -1 ? "" : url.slice(q + 1));
      idx += 1;
      notify();
    },
    back() {
      if (idx > 0) {
        idx -= 1;
        notify();
      }
    },
    forward() {
      if (idx < entries.length - 1) {
        idx += 1;
        notify();
      }
    },
    reset(initialQs = "") {
      entries = [initialQs];
      idx = 0;
      listeners.clear();
    },
  };
  return { navMock };
});

vi.mock("next/navigation", async () => {
  const { useSyncExternalStore } = await import("react");
  return {
    useSearchParams: () => {
      const qs = useSyncExternalStore(navMock.subscribe, navMock.getQs, navMock.getQs);
      return new URLSearchParams(qs);
    },
    usePathname: () => "/admin/memory-health",
    useRouter: () => ({ push: (url: string) => navMock.push(url) }),
  };
});

// Stable translator: `key` or `key:{json}` so param interpolation is visible.
// `.has` mirrors the real message catalog for the note codes under test —
// the page treats the catalog as the single source of known codes.
const CATALOG_NOTE_KEYS = new Set(["notes.judge_failures", "notes.unknown"]);
const stableTranslator = Object.assign(
  (key: string, values?: Record<string, unknown>) => {
    if (values && Object.keys(values).length > 0) {
      return `${key}:${JSON.stringify(values)}`;
    }
    return key;
  },
  { has: (key: string) => !key.startsWith("notes.") || CATALOG_NOTE_KEYS.has(key) },
);

vi.mock("next-intl", () => ({
  useTranslations: (_namespace: string) => stableTranslator,
  useLocale: () => "en",
}));

// ---------- Fixtures ---------------------------------------------------------

const CTX_ID = "11111111-2222-3333-4444-555555555555";

const BREAKDOWN = {
  generated_at: "2026-07-12T00:00:00Z",
  overall_status: "warn",
  contexts: [
    {
      context_id: CTX_ID,
      name: "Context A",
      overall_status: "warn",
      sections: { consolidation: "warn", graph: "ok", retrieval: "ok" },
    },
    {
      context_id: null,
      name: null,
      overall_status: "ok",
      sections: { consolidation: "ok", graph: "ok", retrieval: "ok" },
    },
  ],
};

const DETAIL = {
  generated_at: "2026-07-12T00:00:00Z",
  context_id: CTX_ID,
  context_name: "Context A",
  overall_status: "warn",
  sections: {
    consolidation: {
      status: "warn",
      metrics: { reports_in_window: 3 },
      notes: [
        { code: "judge_failures", params: { count: 2, degraded_runs: 1 } },
        { code: "brand_new_code", params: { anything: true } },
      ],
    },
    graph: {
      status: "ok",
      metrics: { edges_by_origin: { hebbian: 10 } },
      notes: [],
    },
    retrieval: { status: "ok", metrics: { recall_calls: 7 }, notes: [] },
  },
};

beforeEach(() => {
  mockGet.mockReset();
  navMock.reset();
});

// ---------- Tests ------------------------------------------------------------

describe("AdminMemoryHealthPage breakdown (#1225)", () => {
  it("renders one row per context and labels the unattributed bucket", async () => {
    mockGet.mockResolvedValueOnce(BREAKDOWN);

    render(<AdminMemoryHealthPage />);

    await waitFor(() => {
      expect(screen.getByText("Context A")).toBeInTheDocument();
    });
    expect(mockGet).toHaveBeenCalledWith("/api/v1/admin/memory-health");
    expect(screen.getByText("unattributed")).toBeInTheDocument();
    expect(screen.getByTestId(`context-${CTX_ID}`)).toBeInTheDocument();
    expect(screen.getByTestId("context-unattributed")).toBeInTheDocument();
  });

  it("renders the empty state for a zero-context user", async () => {
    mockGet.mockResolvedValueOnce({
      generated_at: "2026-07-12T00:00:00Z",
      overall_status: "ok",
      contexts: [],
    });

    render(<AdminMemoryHealthPage />);

    await waitFor(() => {
      expect(screen.getByText("emptyTitle")).toBeInTheDocument();
    });
  });
});

describe("AdminMemoryHealthPage drill-down (#1225)", () => {
  it("fetches the context detail and renders localized notes with a fallback", async () => {
    mockGet.mockResolvedValueOnce(BREAKDOWN).mockResolvedValueOnce(DETAIL);

    render(<AdminMemoryHealthPage />);
    await waitFor(() => {
      expect(screen.getByTestId(`context-${CTX_ID}`)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId(`context-${CTX_ID}`));

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith(
        `/api/v1/admin/memory-health?context_id=${CTX_ID}`,
      );
    });
    // Known code: localized via its catalog key with params interpolated.
    await waitFor(() => {
      expect(
        screen.getByText('notes.judge_failures:{"count":2,"degraded_runs":1}'),
      ).toBeInTheDocument();
    });
    // Unknown code: generic fallback naming the code — never blank.
    expect(
      screen.getByText('notes.unknown:{"code":"brand_new_code"}'),
    ).toBeInTheDocument();
    // No GitHub issue reference anywhere in the rendered document.
    expect(document.body.textContent).not.toMatch(/#\d{3,}/);
  });

  it("uses the unattributed sentinel for the context-less bucket", async () => {
    mockGet.mockResolvedValueOnce(BREAKDOWN).mockResolvedValueOnce({
      ...DETAIL,
      context_id: null,
      context_name: null,
    });

    render(<AdminMemoryHealthPage />);
    await waitFor(() => {
      expect(screen.getByTestId("context-unattributed")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("context-unattributed"));

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith(
        "/api/v1/admin/memory-health?context_id=unattributed",
      );
    });
  });
});

describe("AdminMemoryHealthPage URL-synced drill-down (#1227)", () => {
  it("deep-links straight into the detail view without a list fetch", async () => {
    // AC: refresh/bookmark of a detail URL renders the detail directly —
    // this is also the page-refresh case (F5 == fresh render with the param).
    navMock.reset(`context_id=${CTX_ID}`);
    mockGet.mockResolvedValueOnce(DETAIL);

    render(<AdminMemoryHealthPage />);

    await waitFor(() => {
      expect(screen.getByText("Context A")).toBeInTheDocument();
    });
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith(`/api/v1/admin/memory-health?context_id=${CTX_ID}`);
  });

  it("deep-links to the unattributed scope like any context", async () => {
    navMock.reset("context_id=unattributed");
    mockGet.mockResolvedValueOnce({ ...DETAIL, context_id: null, context_name: null });

    render(<AdminMemoryHealthPage />);

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith(
        "/api/v1/admin/memory-health?context_id=unattributed",
      );
    });
  });

  it("drill-down pushes the scope into the URL", async () => {
    mockGet.mockResolvedValueOnce(BREAKDOWN).mockResolvedValueOnce(DETAIL);

    render(<AdminMemoryHealthPage />);
    await waitFor(() => {
      expect(screen.getByTestId(`context-${CTX_ID}`)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId(`context-${CTX_ID}`));

    await waitFor(() => {
      expect(navMock.getQs()).toBe(`context_id=${CTX_ID}`);
    });
  });

  it("browser back returns to the retained list without a refetch; forward reloads the detail", async () => {
    mockGet.mockResolvedValueOnce(BREAKDOWN).mockResolvedValueOnce(DETAIL);

    render(<AdminMemoryHealthPage />);
    await waitFor(() => {
      expect(screen.getByTestId(`context-${CTX_ID}`)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId(`context-${CTX_ID}`));
    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledTimes(2);
    });

    // Browser back: the retained breakdown renders instantly — no refetch.
    act(() => {
      navMock.back();
    });
    await waitFor(() => {
      expect(screen.getByTestId(`context-${CTX_ID}`)).toBeInTheDocument();
    });
    expect(mockGet).toHaveBeenCalledTimes(2);

    // Browser forward: back into the detail view (fresh fetch).
    mockGet.mockResolvedValueOnce(DETAIL);
    act(() => {
      navMock.forward();
    });
    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledTimes(3);
    });
    expect(mockGet).toHaveBeenLastCalledWith(
      `/api/v1/admin/memory-health?context_id=${CTX_ID}`,
    );
  });

  it("the in-page back button clears the URL param and shows the retained list", async () => {
    mockGet.mockResolvedValueOnce(BREAKDOWN).mockResolvedValueOnce(DETAIL);

    render(<AdminMemoryHealthPage />);
    await waitFor(() => {
      expect(screen.getByTestId(`context-${CTX_ID}`)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId(`context-${CTX_ID}`));
    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledTimes(2);
    });

    fireEvent.click(screen.getByText("back"));

    await waitFor(() => {
      expect(screen.getByTestId(`context-${CTX_ID}`)).toBeInTheDocument();
    });
    expect(navMock.getQs()).toBe("");
    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});
